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

    @model_validator(mode="after")
    def _check_cpu_layout(self) -> "Settings":
        """Detecta colisión entre cores HTTP y de conversión (causa contention bajo carga)."""
        overlap = set(self.HTTP_CPU_CORES) & set(self.CONVERT_CPU_CORES)
        if overlap:
            raise RuntimeError(
                f"HTTP_CPU_CORES y CONVERT_CPU_CORES se solapan en {sorted(overlap)}. "
                "Los workers HTTP y de conversión deben estar en cores disjuntos para "
                "evitar contención CPU. Revisa la config."
            )
        if self.HTTP_WORKERS < 1:
            raise RuntimeError("HTTP_WORKERS debe ser >= 1.")
        if self.CONVERT_MAX_CONCURRENT < 1:
            raise RuntimeError("CONVERT_MAX_CONCURRENT debe ser >= 1.")
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

    # ── Workers HTTP (FastAPI / uvicorn) ──────────────────────────────────────
    # Número de procesos uvicorn que sirven HTTP. 1 = un solo proceso async
    # (techo ~285 RPS en Ryzen 5 5600X). Subir a 2-4 multiplica throughput pero
    # requiere coordinación con CONVERT_CPU_CORES para no oversuscribir cores.
    # Restricción: incompatible con --reload (uvicorn fuerza workers=1 en dev).
    HTTP_WORKERS: int = 1

    # Núcleos reservados para los workers HTTP (event loop asyncio).
    # Cada worker recibe afinidad sobre uno o más de estos cores. Si el set es
    # más pequeño que HTTP_WORKERS, los excedentes se reparten cíclicamente y
    # algunos workers compartirán core (degradación gradual, no fallo).
    HTTP_CPU_CORES: list[int] = [0, 1]

    # ── OCR (GPU/CPU) ─────────────────────────────────────────────────────────
    # Pre-cargar el modelo EasyOCR al arrancar el servidor (~1.5 s extra de
    # boot) para eliminar el cold start en la primera conversión escaneada.
    # Si CUDA no está disponible, el pre-warm omite silenciosamente.
    OCR_PREWARM: bool = True

    # Páginas procesadas en paralelo dentro de un mismo PDF. Cada hilo renderiza
    # su página en CPU y luego adquiere el semáforo GPU para el OCR. Valor 2-4
    # balancea overlap CPU/GPU sin saturar VRAM. >4 no ayuda (CUDA serializa).
    OCR_PAGES_PARALLEL: int = 3

    # Concurrentes GPU permitidos. RTX 3060 Ti 8 GB: 1.17 GB por modelo cargado,
    # ~50 MB por imagen en VRAM. Hasta 5 sesiones caben holgadas; 3 deja margen
    # para gaming/otros workloads en el mismo equipo.
    OCR_GPU_CONCURRENCY: int = 3

    # Si la cola GPU no se desocupa en este timeout, se cae a OCR-CPU como
    # apoyo. Mantiene throughput bajo carga aunque la página individual sea
    # más lenta. 0 desactiva el fallback (cola infinita).
    OCR_GPU_TIMEOUT_S: float = 8.0

    # DPI de rasterización para OCR. 150 ya es legible; 200 mejora ~5 % de
    # exactitud a costo de ~33 % más tiempo. Subir a 300 para libros muy
    # antiguos o tipografías delicadas.
    OCR_DPI: int = 200

    # ── Conversión de archivos ────────────────────────────────────────────────
    CONVERT_TIMEOUT_SECONDS: int = 300   # Timeout máximo por archivo individual
    # Procesos de conversión TOTALES en el sistema (suma de todos los HTTP workers).
    # ProcessPoolExecutor crea procesos separados → cada uno tiene su propio GIL
    # → paralelismo CPU real. Si HTTP_WORKERS=N, cada worker abre un pool de
    # tamaño ceil(CONVERT_MAX_CONCURRENT / N) para que el total se mantenga.
    CONVERT_MAX_CONCURRENT:  int = 4

    # Núcleos físicos asignados a los workers de conversión.
    # Núcleos 0-1 quedan libres para el event loop asyncio y el SO.
    # Ryzen 5 5600X (6C/12T): núcleos 2-5 para conversión.
    # IMPORTANTE: estos cores DEBEN ser disjuntos de HTTP_CPU_CORES. Si se solapan,
    # los workers HTTP y los de conversión competirán por los mismos cores y el
    # rendimiento bajo carga colapsa (medido: 26× más errores en load test).
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
