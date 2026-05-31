"""
=============================================================================
firebase_auth.py — Autenticación Firebase y control de límites de conversión
=============================================================================
Implementa:
  • Verificación de Firebase ID tokens (RS256) usando JWKS de Google.
    No requiere service account — la clave privada permanece en Google.
  • Caché TTL adaptativo de claves RSA públicas (rotadas ~cada 6 h).
  • Gate de límites de conversión via Firestore REST API, autenticado
    con el propio ID token del usuario (honra las reglas de seguridad).
  • Lock per-UID: elimina condiciones de carrera cuando el mismo usuario
    envía varias peticiones concurrentes en un servidor único.
=============================================================================
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import time
from typing import NamedTuple, Optional

import httpx
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings

logger = logging.getLogger("bookdork.firebase_auth")

# ─────────────────────────────────────────────────────────────────────────────
# Caché JWKS (claves públicas RSA de Firebase)
# ─────────────────────────────────────────────────────────────────────────────

_JWKS_URL = (
    "https://www.googleapis.com/service_accounts/v1/jwk/"
    "securetoken@system.gserviceaccount.com"
)
_KEYS_MIN_TTL  = 300    # Refrescar como mínimo cada 5 min
_KEYS_MAX_TTL  = 21600  # Máximo 6 h (ciclo de rotación de Google)


class _JWKSCache:
    """
    Caché asíncrono thread-safe de claves públicas RSA de Firebase.
    Respeta el TTL real de Cache-Control y reintenta si un kid es desconocido
    (posible rotación de clave reciente).
    """

    def __init__(self) -> None:
        self._keys: dict[str, object] = {}
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_key(self, kid: str) -> object:
        """Devuelve la clave RSA pública para el kid dado."""
        async with self._lock:
            if time.monotonic() >= self._expires_at or kid not in self._keys:
                await self._refresh()

        key = self._keys.get(kid)
        if key is None:
            # Rotación reciente — segundo intento forzado
            async with self._lock:
                await self._refresh()
            key = self._keys.get(kid)

        if key is None:
            raise HTTPException(
                status_code=401,
                detail="Token firmado con clave desconocida. Vuelve a iniciar sesión.",
            )
        return key

    async def _refresh(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(_JWKS_URL)
                resp.raise_for_status()
                data = resp.json()

            # TTL real desde Cache-Control (Google lo incluye)
            ttl = _KEYS_MIN_TTL
            for part in resp.headers.get("cache-control", "").split(","):
                part = part.strip()
                if part.startswith("max-age="):
                    try:
                        ttl = max(_KEYS_MIN_TTL, min(int(part[8:]), _KEYS_MAX_TTL))
                    except ValueError:
                        pass

            import jwt as _pyjwt

            new_keys: dict[str, object] = {}
            for jwk in data.get("keys", []):
                kid = jwk.get("kid")
                if kid:
                    new_keys[kid] = _pyjwt.algorithms.RSAAlgorithm.from_jwk(
                        json.dumps(jwk)
                    )

            self._keys = new_keys
            self._expires_at = time.monotonic() + ttl
            logger.debug("JWKS refrescado: %d claves, TTL=%ds", len(new_keys), ttl)

        except Exception as exc:
            # No borrar caché existente ante un fallo transitorio de red
            logger.error("Error refrescando JWKS: %s", exc)


_jwks_cache = _JWKSCache()


# ─────────────────────────────────────────────────────────────────────────────
# Verificación de Firebase ID token
# ─────────────────────────────────────────────────────────────────────────────

async def verify_firebase_token(token: str) -> dict:
    """
    Verifica un Firebase ID token RS256 y retorna los claims.
    No requiere service account: usa las claves públicas de Google (JWKS).

    Raises:
        HTTPException 401  si el token es inválido, expirado o malformado.
    """
    import jwt as _pyjwt

    try:
        header = _pyjwt.get_unverified_header(token)
    except _pyjwt.DecodeError:
        raise HTTPException(status_code=401, detail="Token de autenticación inválido.")

    kid = header.get("kid")
    alg = header.get("alg")

    if not kid:
        raise HTTPException(status_code=401, detail="Token sin campo 'kid'.")
    if alg != "RS256":
        raise HTTPException(
            status_code=401,
            detail=f"Algoritmo de firma no permitido: {alg}.",
        )

    public_key = await _jwks_cache.get_key(kid)
    settings = get_settings()

    try:
        claims = _pyjwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=settings.FIREBASE_PROJECT_ID,
            options={"verify_exp": True, "verify_iat": True},
        )
    except _pyjwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Sesión expirada. Vuelve a iniciar sesión.",
        )
    except _pyjwt.InvalidAudienceError:
        raise HTTPException(
            status_code=401,
            detail="Sesión inválida. Vuelve a iniciar sesión.",
        )
    except _pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Sesión inválida. Vuelve a iniciar sesión.")

    expected_issuer = (
        f"https://securetoken.google.com/{settings.FIREBASE_PROJECT_ID}"
    )
    if claims.get("iss") != expected_issuer:
        raise HTTPException(
            status_code=401,
            detail="Emisor del token no reconocido.",
        )

    return claims


# ─────────────────────────────────────────────────────────────────────────────
# Dependencia FastAPI: extrae y verifica el Bearer token
# ─────────────────────────────────────────────────────────────────────────────

_bearer_scheme = HTTPBearer(auto_error=False)


class AuthContext(NamedTuple):
    """Token crudo + claims verificados + UID extraído del JWT."""
    raw_token: str
    uid: str
    claims: dict


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> dict:
    """
    Dependencia FastAPI ligera: devuelve los claims del token verificado.
    Usar cuando solo se necesita saber quién es el usuario (no operar Firestore
    en nombre del usuario).
    """
    if not creds or not creds.credentials:
        raise HTTPException(
            status_code=401,
            detail=(
                "Autenticación requerida. "
                "Incluye: Authorization: Bearer <firebase_id_token>"
            ),
        )
    return await verify_firebase_token(creds.credentials)


async def get_auth_context(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> AuthContext:
    """
    Dependencia FastAPI completa: devuelve token crudo + UID + claims.
    Usar cuando el endpoint necesita operar Firestore REST API en nombre
    del usuario (ej. leer/escribir límites de conversión).
    """
    if not creds or not creds.credentials:
        raise HTTPException(
            status_code=401,
            detail=(
                "Autenticación requerida. "
                "Incluye: Authorization: Bearer <firebase_id_token>"
            ),
        )
    claims = await verify_firebase_token(creds.credentials)
    # Firebase pone el UID en el claim estándar "sub"
    uid = claims.get("sub") or claims.get("user_id") or ""
    if not uid:
        raise HTTPException(status_code=401, detail="Token sin UID de usuario.")
    return AuthContext(raw_token=creds.credentials, uid=uid, claims=claims)


# ─────────────────────────────────────────────────────────────────────────────
# Control de límites de conversión — Admin SDK
# ─────────────────────────────────────────────────────────────────────────────

_PLAN_LIMITS: dict[str, dict] = {
    "gratis": {"type": "lifetime", "max": 5,   "max_file_bytes": 20  * 1024 * 1024},
    "basic":  {"type": "daily",    "max": 50,  "max_file_bytes": 50  * 1024 * 1024},
    "pro":    {"type": "daily",    "max": 200, "max_file_bytes": 200 * 1024 * 1024},
}

# Lock por UID: evita que dos peticiones concurrentes del mismo usuario lean
# "5 restantes" antes de que ninguna escriba el decremento.
# Capacidad máxima para prevenir agotamiento de memoria (MED-04).
_UID_LOCKS_MAX = 20_000
_uid_locks: dict[str, asyncio.Lock] = {}
_uid_locks_global = asyncio.Lock()


async def _get_uid_lock(uid: str) -> asyncio.Lock:
    async with _uid_locks_global:
        if uid not in _uid_locks:
            # Limpiar la mitad más antigua cuando se alcanza el límite
            if len(_uid_locks) >= _UID_LOCKS_MAX:
                evict_count = _UID_LOCKS_MAX // 2
                for k in list(_uid_locks.keys())[:evict_count]:
                    del _uid_locks[k]
                logger.debug("UID locks: evicted %d entries (capacity limit).", evict_count)
            _uid_locks[uid] = asyncio.Lock()
        return _uid_locks[uid]


def _remaining_conversions(data: dict, today: str) -> int:
    """Calcula las conversiones disponibles para un usuario dado su plan."""
    plan   = data.get("plan") or "gratis"
    limits = _PLAN_LIMITS.get(plan, _PLAN_LIMITS["gratis"])

    if limits["type"] == "lifetime":
        return max(0, limits["max"] - (data.get("conversiones") or 0))

    date  = data.get("fechaConversiones") or ""
    count = (data.get("conversionesDiarias") or 0) if date == today else 0
    return max(0, limits["max"] - count)


async def check_and_reserve(
    uid: str,
    num_files: int,
) -> int:
    """
    Verifica el límite de conversiones del usuario y reserva slots
    de forma atómica para la petición actual.

    Usa el Admin SDK para leer y escribir Firestore (privilegiado),
    evitando que las reglas de seguridad de Firestore bloqueen la escritura.

    Flujo:
      1. Adquiere el lock per-UID (serializa peticiones del mismo usuario).
      2. Lee el documento de Firestore vía Admin SDK.
      3. Calcula cuántos slots quedan según el plan.
      4. Escribe el incremento inmediatamente (reserva optimista).

    Args:
        uid:       UID del usuario autenticado (del JWT).
        num_files: Número de archivos que el usuario quiere convertir.

    Returns:
        Número de slots reservados (puede ser < num_files si el plan está
        casi agotado pero aún tiene alguno disponible).

    Raises:
        HTTPException 402  si el límite del plan está completamente agotado.
        HTTPException 503  si Firestore no responde o el Admin SDK no está inicializado.
    """
    from .firebase_admin_client import get_firestore_user, update_user_conversions

    today    = datetime.date.today().isoformat()
    uid_lock = await _get_uid_lock(uid)

    async with uid_lock:
        # ── 1. Leer documento del usuario ─────────────────────────────────────
        try:
            data = await get_firestore_user(uid)
        except RuntimeError as exc:
            logger.error("Firestore read error uid=%s: %s", uid[:8], exc)
            raise HTTPException(
                status_code=503,
                detail="No se puede verificar el límite de conversiones. Intenta más tarde.",
            )

        # ── 2. Calcular slots disponibles ─────────────────────────────────────
        remaining = _remaining_conversions(data, today)
        plan      = data.get("plan") or "gratis"

        if remaining <= 0:
            raise HTTPException(
                status_code=402,
                detail={
                    "error":     "limit_exceeded",
                    "message":   (
                        f"Límite de conversiones agotado para el plan '{plan}'. "
                        "Actualiza tu plan en /plans."
                    ),
                    "plan":      plan,
                    "remaining": 0,
                },
            )

        slots = min(num_files, remaining)

        # ── 3. Reservar inmediatamente (write-before-process) ─────────────────
        limits = _PLAN_LIMITS.get(plan, _PLAN_LIMITS["gratis"])

        if limits["type"] == "lifetime":
            current = data.get("conversiones") or 0
            fields  = {"conversiones": current + slots}
        else:
            date    = data.get("fechaConversiones") or ""
            current = (data.get("conversionesDiarias") or 0) if date == today else 0
            fields  = {
                "conversionesDiarias": current + slots,
                "fechaConversiones":   today,
            }

        try:
            await update_user_conversions(uid, fields)
        except RuntimeError as exc:
            logger.error("Firestore write error uid=%s: %s", uid[:8], exc)
            raise HTTPException(
                status_code=503,
                detail="Error registrando conversión. Intenta más tarde.",
            )

        logger.info(
            "Conversion reserved — uid=%s plan=%s slots=%d remaining_after=%d",
            uid[:8], plan, slots, remaining - slots,
        )
        return slots


async def get_user_plan_limits(uid: str) -> dict:
    """Devuelve la entrada de _PLAN_LIMITS para el plan actual del usuario."""
    from .firebase_admin_client import get_firestore_user
    try:
        data = await get_firestore_user(uid)
        plan = data.get("plan") or "gratis"
    except Exception:
        plan = "gratis"
    return _PLAN_LIMITS.get(plan, _PLAN_LIMITS["gratis"])
