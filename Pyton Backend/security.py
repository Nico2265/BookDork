"""
=============================================================================
security.py — Capa de seguridad de BookDork
=============================================================================
Implementa:
  • Rate limiting por IP (token bucket LRU con capacidad máxima)
  • Cabeceras de seguridad HTTP (CSP, HSTS, COOP, CORP…)
  • X-Request-ID por petición (trazabilidad en auditoría e incidentes)
  • Validación de API key para endpoints administrativos
  • Anuncio HTTP/3 via Alt-Svc (solo cuando TLS está activo)

Cambios de seguridad aplicados (2024):
  - Eliminado 'unsafe-inline' de style-src (HIGH-01 / OWASP A03)
  - Rate limiter LRU con capacidad máxima para prevenir DoS de memoria
  - Añadido X-Permitted-Cross-Domain-Policies: none
  - Añadido Cache-Control: no-store en rutas /api/
  - X-Request-ID generado por petición para trazabilidad de auditoría
  - User-Agent check documentado como disuasivo superficial (no reemplaza auth)
=============================================================================
"""

import hashlib
import logging
import secrets
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from fastapi import Request, HTTPException, Security
from fastapi.security import APIKeyHeader
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .config import get_settings

logger = logging.getLogger("bookdork.security")


# ─────────────────────────────────────────────────────────────────────────────
# Rate Limiter (Token Bucket LRU por IP)
# ─────────────────────────────────────────────────────────────────────────────

class _RateLimiter:
    """
    Token Bucket por IP con capacidad máxima (LRU eviction).

    Cuando el número de IPs distintas alcanza max_buckets, se elimina la
    entrada menos recientemente usada para evitar agotamiento de memoria bajo
    ataques de IP rotation (MED-04).

    Usar únicamente vía get_rate_limiter().
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: int = 60,
        max_buckets: int = 50_000,
    ) -> None:
        self._max         = max_requests
        self._window      = window_seconds
        self._max_buckets = max_buckets
        # OrderedDict mantiene orden de inserción/acceso para evicción LRU
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    @staticmethod
    def _hash_ip(ip: str) -> str:
        return hashlib.sha256(ip.encode()).hexdigest()[:16]

    def is_allowed(self, ip: str) -> tuple[bool, int]:
        key = self._hash_ip(ip)
        now = time.monotonic()

        if key in self._buckets:
            tokens, last = self._buckets[key]
            self._buckets.move_to_end(key)       # Actualizar posición LRU
        else:
            # Evicción LRU si al límite de capacidad
            if len(self._buckets) >= self._max_buckets:
                self._buckets.popitem(last=False)  # Eliminar el más antiguo
            tokens, last = float(self._max), now

        tokens = min(self._max, tokens + (now - last) * (self._max / self._window))

        if tokens >= 1:
            self._buckets[key] = (tokens - 1, now)
            return True, 0

        wait = int((1 - tokens) * self._window / self._max) + 1
        self._buckets[key] = (tokens, now)
        return False, wait

    def cleanup_old_entries(self, max_age_seconds: int = 3600) -> None:
        """Elimina entradas sin actividad reciente. Llamar periódicamente."""
        now   = time.monotonic()
        stale = [k for k, (_, ts) in self._buckets.items() if now - ts > max_age_seconds]
        for k in stale:
            del self._buckets[k]
        if stale:
            logger.debug("Rate limiter: %d entradas antiguas eliminadas.", len(stale))


# ─────────────────────────────────────────────────────────────────────────────
# Middleware de Seguridad HTTP
# ─────────────────────────────────────────────────────────────────────────────

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Añade cabeceras de seguridad OWASP a todas las respuestas y genera un
    X-Request-ID único por petición para trazabilidad en auditoría.

    Cabeceras aplicadas:
      Content-Security-Policy       — sin 'unsafe-inline' en style-src
      X-Content-Type-Options        — nosniff
      X-Frame-Options               — DENY
      Referrer-Policy               — strict-origin-when-cross-origin
      Permissions-Policy            — mínimo requerido
      Cross-Origin-Opener-Policy    — same-origin-allow-popups
      Cross-Origin-Resource-Policy  — same-origin
      X-Permitted-Cross-Domain-Policies — none
      Cache-Control                 — no-store en rutas /api/
      X-Request-ID                  — UUID hex por petición
      Strict-Transport-Security     — solo con TLS activo
      Alt-Svc                       — HTTP/3, solo con TLS + sin proxy
    """

    def __init__(self, app) -> None:
        super().__init__(app)
        self._settings = get_settings()
        cert = Path(self._settings.TLS_CERT_FILE)
        key  = Path(self._settings.TLS_KEY_FILE)
        self._tls_active = cert.exists() and key.exists()
        proto = "https" if self._tls_active else "http"
        port  = self._settings.HTTP3_PORT

        # CSP endurecida: eliminado 'unsafe-inline' de style-src (OWASP A03)
        self._csp = (
            "default-src 'self'; "
            "script-src 'self' https://www.gstatic.com https://www.google.com "
            "https://recaptcha.google.com; "
            "style-src 'self' https://fonts.googleapis.com https://fonts.gstatic.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https: http:; "
            f"connect-src 'self' {proto}://localhost:{port} {proto}://127.0.0.1:{port} "
            "https://www.gstatic.com https://www.google.com https://recaptcha.google.com "
            "https://openlibrary.org https://covers.openlibrary.org "
            "https://*.googleapis.com https://*.firebaseio.com "
            "https://securetoken.googleapis.com https://identitytoolkit.googleapis.com "
            "https://firestore.googleapis.com; "
            "frame-src 'self' https://www.gstatic.com https://*.firebaseapp.com "
            "https://bookdork-b9825.firebaseapp.com "
            "https://www.google.com https://recaptcha.google.com; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "upgrade-insecure-requests;"
        )

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Generar o propagar X-Request-ID (trazabilidad de auditoría)
        request_id = request.headers.get("X-Request-ID", "").strip()
        if not request_id or len(request_id) > 64:
            request_id = uuid.uuid4().hex
        request.state.request_id = request_id

        response = await call_next(request)

        # ── Cabeceras de seguridad universales ─────────────────────────────────
        response.headers["Content-Security-Policy"]           = self._csp
        response.headers["X-Content-Type-Options"]            = "nosniff"
        response.headers["X-Frame-Options"]                   = "DENY"
        response.headers["Referrer-Policy"]                   = "strict-origin-when-cross-origin"
        response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), payment=(), "
            "usb=(), display-capture=(), interest-cohort=()"
        )
        response.headers["Cross-Origin-Opener-Policy"]   = "same-origin-allow-popups"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["X-Request-ID"]                 = request_id

        # Cache-Control: no-store en endpoints de API (datos autenticados / sensibles)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"

        # ── Cabeceras TLS-dependientes ─────────────────────────────────────────
        if self._tls_active:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )
            is_proxied = bool(
                request.headers.get("x-forwarded-for")
                or request.headers.get("x-forwarded-host")
            )
            if not is_proxied:
                response.headers["Alt-Svc"] = (
                    f'h3=":{self._settings.HTTP3_PORT}"; ma=86400'
                )
            else:
                response.headers["Alt-Svc"] = "clear"
        else:
            response.headers["Alt-Svc"] = "clear"

        # ── Eliminar cabeceras que revelan el stack tecnológico ────────────────
        for h in ("server", "x-powered-by"):
            if h in response.headers:
                del response.headers[h]

        return response


# ─────────────────────────────────────────────────────────────────────────────
# Middleware de Rate Limiting
# ─────────────────────────────────────────────────────────────────────────────

_rate_limiter: _RateLimiter | None = None

_RATE_LIMIT_EXCLUDED = frozenset({"/api/health", "/", "/search", "/converter", "/auth", "/plans"})


def get_rate_limiter() -> _RateLimiter:
    """Singleton: inicializa el limitador una sola vez."""
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = _RateLimiter(
            max_requests=get_settings().RATE_LIMIT_PER_MINUTE,
            window_seconds=60,
            max_buckets=50_000,
        )
    return _rate_limiter


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Limita peticiones por IP con LRU eviction para prevenir DoS de memoria.

    Con TRUSTED_PROXY_DEPTH=0 (por defecto) usa la IP TCP directa e ignora
    X-Forwarded-For para prevenir IP spoofing (MED-03).
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        depth = get_settings().TRUSTED_PROXY_DEPTH
        if depth > 0:
            forwarded = request.headers.get("X-Forwarded-For", "")
            ips = [ip.strip() for ip in forwarded.split(",") if ip.strip()]
            client_ip = ips[-depth] if len(ips) >= depth else (
                request.client.host if request.client else "unknown"
            )
        else:
            client_ip = request.client.host if request.client else "unknown"

        if request.url.path in _RATE_LIMIT_EXCLUDED or request.url.path.startswith("/static/"):
            return await call_next(request)

        allowed, retry_after = get_rate_limiter().is_allowed(client_ip)
        if not allowed:
            rid = getattr(request.state, "request_id", "-")
            logger.warning(
                "Rate limit excedido — ip_hash=%s ruta=%s request_id=%s",
                hashlib.sha256(client_ip.encode()).hexdigest()[:8],
                request.url.path,
                rid,
            )
            return Response(
                content='{"detail":"Demasiadas solicitudes. Intenta más tarde."}',
                status_code=429,
                media_type="application/json",
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)


# ─────────────────────────────────────────────────────────────────────────────
# Validación de API Key (endpoints admin)
# ─────────────────────────────────────────────────────────────────────────────

_api_key_header = APIKeyHeader(name="X-Admin-API-Key", auto_error=False)


async def require_admin_key(
    api_key: str | None = Security(_api_key_header),
) -> str:
    """Verifica la clave de administrador con comparación en tiempo constante."""
    settings = get_settings()
    if not api_key or not secrets.compare_digest(
        api_key.encode(), settings.ADMIN_API_KEY.encode()
    ):
        logger.warning("Intento de acceso admin con clave inválida")
        raise HTTPException(
            status_code=403,
            detail="Acceso denegado.",
        )
    return api_key


# ─────────────────────────────────────────────────────────────────────────────
# Disuasivo de User-Agent (primer filtro superficial, NO es un control de seguridad)
# ─────────────────────────────────────────────────────────────────────────────

_BLOCKED_AGENTS = frozenset({
    "python-requests/",
    "scrapy/",
    "libwww-perl/",
    "go-http-client/",
    "java/",
})


def check_user_agent(request: Request) -> None:
    """
    Disuasivo de primer nivel contra scrapers automatizados que no falsifican UA.
    ADVERTENCIA: trivialmente eludible — no es un control de seguridad real.
    La autenticación Firebase es la capa de seguridad efectiva.
    """
    ua = request.headers.get("User-Agent", "").lower()
    if not ua:
        raise HTTPException(status_code=400, detail="Cabecera User-Agent requerida.")
    for blocked in _BLOCKED_AGENTS:
        if blocked in ua:
            raise HTTPException(status_code=403, detail="Cliente no permitido.")
