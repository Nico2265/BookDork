"""
=============================================================================
config.py — Configuración central de la aplicación BookDork
=============================================================================
Carga variables de entorno y expone una instancia única de Settings.
Todas las claves sensibles se leen desde .env (nunca hardcodeadas).
=============================================================================
"""

import os
from functools import lru_cache
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_MEILI_KEY = "changeme_strong_master_key"
_DEFAULT_ADMIN_KEY = "changeme_admin_api_key"


class Settings(BaseSettings):
    """
    Parámetros de configuración de la aplicación.
    Se leen en orden de prioridad:
      1. Variables de entorno del sistema operativo
      2. Archivo .env en la raíz del proyecto
      3. Valores por defecto definidos aquí
    """

    # ── Aplicación ──────────────────────────────────────────────────────────
    APP_NAME: str = "BookDork Search Engine"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False                       # Nunca True en producción
    APP_DOMAIN: str = ""                      # Dominio de producción, ej: bookdork.com
    NGROK_URL:  str = ""                      # Túnel ngrok temporal, ej: https://abc123.ngrok-free.app
    ALLOWED_ORIGINS: list[str] = [            # CORS — cubre HTTP (dev) y HTTPS (prod/HTTP3)
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "https://localhost:8000",
        "https://127.0.0.1:8000",
        "https://localhost:8443",
        "https://127.0.0.1:8443",
        "https://localhost",
        "https://127.0.0.1",
    ]

    @property
    def cors_origins(self) -> list[str]:
        origins = list(self.ALLOWED_ORIGINS)
        if self.APP_DOMAIN:
            origins += [
                f"https://{self.APP_DOMAIN}",
                f"https://www.{self.APP_DOMAIN}",
            ]
        if self.NGROK_URL:
            origins.append(self.NGROK_URL.rstrip("/"))
        return origins

    # ── HTTP/3 / TLS ─────────────────────────────────────────────────────────
    HOST: str = "0.0.0.0"
    HTTP3_PORT: int = 8443                    # Puerto QUIC/HTTP3
    TLS_CERT_FILE: str = "certs/server.crt"  # Certificado TLS (obligatorio para HTTP/3)
    TLS_KEY_FILE: str = "certs/server.key"   # Clave privada TLS
    FORCE_HTTP: bool = False                  # True = HTTP/1.1 sin TLS (para ngrok/dev)

    # ── Meilisearch ──────────────────────────────────────────────────────────
    MEILI_HOST: str = "http://localhost:7700"
    MEILI_MASTER_KEY: str = "changeme_strong_master_key"  # Cambiar en producción
    MEILI_INDEX_NAME: str = "books"
    MEILI_SEARCH_API_KEY: str = ""            # Clave de solo lectura para el cliente

    # ── Seguridad ────────────────────────────────────────────────────────────
    ADMIN_API_KEY: str = "changeme_admin_api_key"  # Para endpoints de indexación
    RATE_LIMIT_PER_MINUTE: int = 30           # Máx. peticiones por IP por minuto
    MAX_QUERY_LENGTH: int = 512              # Caracteres máximos en la consulta
    MAX_RESULTS_PER_PAGE: int = 50           # Límite de resultados por página
    # Cuántos proxies reversos de confianza hay delante del servidor.
    # 0 = conexión directa (ignorar X-Forwarded-For).
    # 1 = un proxy (Caddy, Nginx, ngrok…) añade la IP real como última entrada de XFF.
    TRUSTED_PROXY_DEPTH: int = 0

    @model_validator(mode="after")
    def _check_production_keys(self) -> "Settings":
        if not self.DEBUG:
            if self.MEILI_MASTER_KEY == _DEFAULT_MEILI_KEY:
                raise RuntimeError(
                    "MEILI_MASTER_KEY usa el valor por defecto inseguro. "
                    "Define una clave fuerte en .env antes de arrancar en producción."
                )
            if self.ADMIN_API_KEY == _DEFAULT_ADMIN_KEY:
                raise RuntimeError(
                    "ADMIN_API_KEY usa el valor por defecto inseguro. "
                    "Define una clave fuerte en .env antes de arrancar en producción."
                )
        return self

    # ── Firebase ─────────────────────────────────────────────────────────────
    FIREBASE_PROJECT_ID: str = "bookdork-b9825"  # ID del proyecto Firebase

    # ── Firebase Admin SDK (service account) ────────────────────────────────
    # Credenciales con prioridad: JSON literal > ruta de archivo.
    # Obtener desde: Firebase Console → Configuración del proyecto
    #                → Cuentas de servicio → Generar nueva clave privada.
    # Si ambos están vacíos el servidor arranca normalmente pero los
    # endpoints de /api/admin/ responderán 503 hasta que se configuren.
    FIREBASE_SERVICE_ACCOUNT_PATH: str = ""   # Ruta al archivo .json
    FIREBASE_SERVICE_ACCOUNT_JSON: str = ""   # Contenido JSON (útil en Docker/CI)

    # ── Conversión de archivos ────────────────────────────────────────────────
    CONVERT_TIMEOUT_SECONDS: int = 300   # Timeout máximo por archivo individual
    # 4 procesos worker = 4 núcleos dedicados (ProcessPoolExecutor, no threads).
    # Cada proceso tiene su propio GIL → paralelismo CPU real.
    CONVERT_MAX_CONCURRENT:  int = 4

    # Núcleos físicos asignados a los workers de conversión.
    # Núcleos 0-1 quedan libres para el event loop asyncio y el SO.
    # Ryzen 5 5600X (6C/12T): núcleos 2-5 para conversión.
    CONVERT_CPU_CORES: list[int] = [2, 3, 4, 5]

    # ── Caché de conversiones ─────────────────────────────────────────────────
    CACHE_DIR:     str  = "conversion_cache"  # Directorio raíz del caché en disco
    CACHE_ENABLED: bool = True                # False = omite caché sin borrar archivos

    # ── Google Dorks ─────────────────────────────────────────────────────────
    GOOGLE_BASE_URL: str = "https://www.google.com/search"
    DORK_SAFE_SEARCH: bool = True            # Añade &safe=active a la URL

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


@lru_cache()
def get_settings() -> Settings:
    """
    Devuelve la instancia singleton de Settings.
    Usar como dependencia de FastAPI:
        settings = Depends(get_settings)
    """
    return Settings()
