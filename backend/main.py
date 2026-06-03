"""
=============================================================================
main.py — Aplicación FastAPI principal con soporte HTTP/3 (QUIC)
=============================================================================
Servidor web del motor de búsqueda BookDork.

HTTP/3 se implementa via Hypercorn (servidor ASGI con soporte QUIC):
  pip install hypercorn[h3]   # Instala el soporte QUIC/HTTP3

Para ejecutar:
  python -m backend.main
  # o directamente con Hypercorn:
  hypercorn backend.main:app --bind 0.0.0.0:8443 \
    --certfile certs/server.crt --keyfile certs/server.key

Endpoints:
  GET  /                           → Sirve el frontend (index.html)
  GET  /api/health                 → Estado del sistema
  GET  /api/search                 → Búsqueda de libros
  GET  /api/dork                   → Genera consulta Dork sin buscar
  POST /api/index/book             → Indexa un libro (requiere admin key)
  GET  /api/index/stats            → Estadísticas del índice (requiere admin key)
=============================================================================
"""

import asyncio
import concurrent.futures
import logging
import multiprocessing
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .dork_engine import BookDorkEngine
from .meilisearch_client import get_meili_client
from .models import (
    BookResult,
    HealthResponse,
    IndexBookRequest,
    SearchRequest,
    SearchResponse,
)
from .firebase_auth import (
    AuthContext,
    Role,
    check_and_reserve,
    get_auth_context,
    get_user_plan_limits,
)
from .firebase_admin_client import initialize_admin_sdk
from .admin_routes import router as admin_router
from .security import (
    AdminPrincipal,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    check_user_agent,
    require_admin_access,
)
# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# pdfminer emite un WARNING por PDFs con el flag "no-extract" en los metadatos,
# pero lo ignora y extrae el texto de igual manera — es ruido innecesario en logs.
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logger = logging.getLogger("bookdork.main")

# ─────────────────────────────────────────────────────────────────────────────
# Startup / Shutdown (ciclo de vida de la aplicación)
# ─────────────────────────────────────────────────────────────────────────────
_app_start_time: float = 0.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Código que se ejecuta al arrancar y al apagar la aplicación.
    - Arranque: inicializa Meilisearch, precalienta caches
    - Apagado: cierra conexiones limpiamente
    """
    global _app_start_time
    _app_start_time = time.monotonic()
    settings = get_settings()

    logger.info("═" * 60)
    logger.info("  %s v%s arrancando…", settings.APP_NAME, settings.APP_VERSION)
    logger.info("  Puerto HTTP/3: %d", settings.HTTP3_PORT)
    logger.info("═" * 60)

    # Inicializar Firebase Admin SDK (operaciones privilegiadas de backend)
    try:
        initialize_admin_sdk()
        logger.info("✓ Firebase Admin SDK inicializado.")
    except RuntimeError as exc:
        logger.warning("⚠ Firebase Admin SDK no configurado: %s", exc)
        logger.warning("  Define FIREBASE_SERVICE_ACCOUNT_PATH o _JSON en .env.")
        logger.warning("  Los endpoints /api/admin/ responderán 503 hasta configurarlo.")

    # Inicializar Meilisearch
    meili = get_meili_client()
    connected = meili.initialize()
    if connected:
        logger.info("✓ Meilisearch conectado y configurado.")
    else:
        logger.warning("⚠ Meilisearch NO disponible. Búsqueda local desactivada.")

    # ── Dimensionado del pool de conversión por worker HTTP ───────────────────
    # Con HTTP_WORKERS=N, cada worker abre su propio ProcessPoolExecutor.
    # Para que la suma de procesos de conversión no exceda CONVERT_MAX_CONCURRENT
    # (que debe ≤ len(CONVERT_CPU_CORES)), repartimos proporcionalmente.
    n_http = max(1, settings.HTTP_WORKERS)
    pool_size_per_worker = max(1, settings.CONVERT_MAX_CONCURRENT // n_http)
    # El semáforo per-worker limita conversiones simultáneas dentro de este proceso.
    global _convert_sem, _convert_pool
    _convert_sem = asyncio.Semaphore(pool_size_per_worker)
    logger.info(
        "✓ Semáforo de conversiones (este worker): %d simultáneas "
        "(total cluster: %d, repartido entre %d workers HTTP).",
        pool_size_per_worker, settings.CONVERT_MAX_CONCURRENT, n_http,
    )

    # Sub-set de CONVERT_CPU_CORES asignado a este worker HTTP, calculado por
    # el slot que le toca al proceso (ver _claim_http_slot). Slot=0 toma los
    # primeros K cores, slot=1 los siguientes, etc., cíclicamente.
    my_http_slot = _claim_http_slot(n_http)
    my_convert_cores = _split_cores(settings.CONVERT_CPU_CORES, n_http, my_http_slot)

    # Pool de procesos con afinidad CPU. Cada proceso del pool se fija a un
    # core distinto del subset asignado a este HTTP worker.
    _worker_counter = multiprocessing.Value("i", 0)
    _convert_pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=max(1, len(my_convert_cores)),
        initializer=_worker_affinity_init,
        initargs=(_worker_counter, list(my_convert_cores)),
    )
    logger.info(
        "✓ Pool de conversión: %d procesos en cores %s (HTTP worker slot=%d).",
        max(1, len(my_convert_cores)), my_convert_cores, my_http_slot,
    )

    # ── Pre-warm del pool de procesos ─────────────────────────────────────────
    # En Windows el ProcessPoolExecutor usa 'spawn': cada worker reimporta
    # backend.pdf_engine al crearse (lazy, en el primer submit). Sin pre-warm,
    # las primeras N conversiones pagan ese arranque (~import de fitz + módulo).
    # Enviamos una tarea trivial por proceso para forzar el spawn AHORA, de modo
    # que el primer usuario reciba un pool ya caliente.
    if settings.CONVERT_POOL_PREWARM:
        n_workers = max(1, len(my_convert_cores))
        try:
            warm_futures = [
                _convert_pool.submit(_pool_warmup) for _ in range(n_workers)
            ]
            for f in warm_futures:
                f.result(timeout=60)
            logger.info("✓ Pool de conversión pre-calentado (%d procesos listos).", n_workers)
        except Exception as exc:
            logger.warning("Pre-warm del pool falló (%s) — los workers se crearán lazy.", exc)

    # Afinidad del event loop: cada worker HTTP toma un core de HTTP_CPU_CORES.
    # Si hay menos cores que workers, se reparten cíclicamente (varios workers
    # comparten core → degradación gradual, no fallo).
    try:
        import psutil as _ps
        http_cores_list = list(settings.HTTP_CPU_CORES)
        if http_cores_list:
            my_http_core = http_cores_list[my_http_slot % len(http_cores_list)]
            _ps.Process().cpu_affinity([my_http_core])
            logger.info(
                "✓ Event loop fijado a core %d (slot=%d, %d HTTP cores disponibles).",
                my_http_core, my_http_slot, len(http_cores_list),
            )
    except Exception as exc:
        logger.debug("CPU affinity no aplicada al event loop: %s", exc)

    # Inicializar caché de conversiones
    if settings.CACHE_ENABLED:
        _cache = get_cache(settings.CACHE_DIR)
        cs = _cache.stats()
        logger.info(
            "✓ Caché de conversiones: '%s'  (%d entradas, %.1f MB en disco).",
            settings.CACHE_DIR, cs["entries"], cs["disk_bytes"] / 1_048_576,
        )
    else:
        logger.info("○ Caché desactivado (CACHE_ENABLED=false).")

    # ── OCR config + pre-warm ─────────────────────────────────────────────────
    # configure() inyecta los valores de Settings al runtime de pdf_engine
    # sin que ese módulo importe config (evita import circular).
    pdf_engine.configure(
        dpi=settings.OCR_DPI,
        pages_parallel=settings.OCR_PAGES_PARALLEL,
        gpu_concurrency=settings.OCR_GPU_CONCURRENCY,
        gpu_timeout_s=settings.OCR_GPU_TIMEOUT_S,
        force_gpu=settings.OCR_FORCE_GPU,
    )

    # Pre-cargar modelo EasyOCR: elimina ~1.5 s de cold start en la primera
    # conversión escaneada. Solo si CUDA disponible y OCR_PREWARM=True.
    # Fallback silencioso si falla (server arranca igual, OCR lazy en demanda).
    if settings.OCR_PREWARM:
        try:
            ok = pdf_engine.prewarm()
            if ok:
                logger.info("✓ OCR pre-warmed (modelo en VRAM/RAM, listo para inferencia).")
            else:
                logger.info("○ OCR no pre-warmed (easyocr no instalado o sin CUDA).")
        except Exception as exc:
            logger.warning("Pre-warm OCR falló: %s — se cargará lazy en demanda.", exc)

    # Estado de los motores de conversión
    eng = pdf_engine.get_engine_status()
    logger.info(
        "✓ Motores activos — PyMuPDF: %s | CUDA: %s | MarkItDown: %s | "
        "OCR-GPU: %s | OCR-CPU: %s",
        eng["pymupdf"], eng["cuda"], eng["markitdown"],
        eng["ocr_gpu_ready"], eng["ocr_cpu_ready"],
    )

    # Tarea periódica: limpiar rate limiter cada hora
    async def cleanup_rate_limiter():
        from .security import get_rate_limiter
        while True:
            await asyncio.sleep(3600)
            get_rate_limiter().cleanup_old_entries()
            logger.debug("Rate limiter: entradas antiguas eliminadas.")

    cleanup_task = asyncio.create_task(cleanup_rate_limiter())

    yield  # La aplicación está en ejecución

    # ── Apagado ──
    cleanup_task.cancel()
    if _convert_pool is not None:
        _convert_pool.shutdown(wait=False, cancel_futures=True)
    logger.info("BookDork apagado correctamente.")


# ─────────────────────────────────────────────────────────────────────────────
# Motores de conversión y caché
# ─────────────────────────────────────────────────────────────────────────────
from . import pdf_engine
from .conversion_cache import (
    ConversionCache,
    classify_topic,
    clean_title,
    detect_language,
    extract_author_from_text,
    extract_edition_from_text,
    extract_isbn,
    extract_year_from_text,
    get_cache,
)

_CONVERT_MAX_BYTES_ABSOLUTE = 200 * 1024 * 1024  # techo absoluto del servidor (plan Pro)
_MAX_FILES         = 5
_ALLOWED_BOOK_EXTS = frozenset({".pdf", ".epub", ".mobi", ".azw3", ".djvu", ".txt"})
_CHUNK_SIZE        = 256 * 1024          # 256 KB por chunk al leer en streaming


async def _fetch_isbn_meta(isbn: str) -> dict:
    """
    Busca metadatos del libro en Open Library por ISBN.
    Usa el endpoint Books API (jscmd=details) que devuelve descripción completa.
    Retorna {} si el ISBN no se encuentra o la petición falla.
    """
    import httpx

    bib_key = f"ISBN:{isbn}"
    try:
        async with httpx.AsyncClient(timeout=settings.ISBN_LOOKUP_TIMEOUT_S) as client:
            resp = await client.get(
                "https://openlibrary.org/api/books",
                params={"bibkeys": bib_key, "format": "json", "jscmd": "details"},
                headers={"Accept": "application/json", "User-Agent": "BookDork/1.0"},
            )
            resp.raise_for_status()
            data = resp.json()

        if bib_key not in data:
            return {}

        entry   = data[bib_key]
        details = entry.get("details", {})

        desc = details.get("description") or ""
        if isinstance(desc, dict):
            desc = desc.get("value", "")
        desc = str(desc).strip()[:800]

        authors   = [a.get("name", "") for a in details.get("authors", []) if a.get("name")]
        subjects  = [s for s in details.get("subjects", [])[:8] if isinstance(s, str)]
        publishers = details.get("publishers") or []
        cover_url = entry.get("thumbnail_url", "").replace("-S.", "-M.")

        return {
            "title":        details.get("title", ""),
            "authors":      authors,
            "description":  desc,
            "subjects":     subjects,
            "publisher":    publishers[0] if publishers else "",
            "publish_date": details.get("publish_date", ""),
            "pages":        details.get("number_of_pages"),
            "cover_url":    cover_url,
            "info_url":     entry.get("info_url", ""),
        }
    except Exception as exc:
        logger.debug("ISBN meta fetch failed for %s: %s", isbn, exc)
        return {}


# ── Persistencia y enriquecimiento fuera de la ruta crítica ──────────────────

# Tareas de fondo vivas: guardamos una referencia fuerte para que el GC no las
# cancele antes de terminar (asyncio solo mantiene weakrefs a las tasks).
_BG_TASKS: set[asyncio.Task] = set()


def _spawn_bg(coro) -> None:
    """Lanza una corrutina fire-and-forget con referencia fuerte y log de errores."""
    task = asyncio.ensure_future(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


def _persist_conversion(
    cache, file_hash: str, md: str, name: str,
    file_size: int, isbn: Optional[str], engine: Optional[str],
) -> str:
    """Clasifica y guarda una conversión en la caché. CPU-bound (regex/sqlite),

    pensado para ejecutarse vía asyncio.to_thread y no bloquear el event loop.
    La portada se rellena después en background (cover_url=None aquí).
    """
    return cache.put(
        file_hash = file_hash,
        markdown  = md,
        filename  = name,
        file_size = file_size,
        isbn      = isbn,
        language  = detect_language(md),
        topic     = classify_topic(name, md),
        engine    = engine,
        title     = clean_title(name),
        author    = extract_author_from_text(name, md[:6_000]),
        year      = extract_year_from_text(name, md[:6_000]),
        edition   = extract_edition_from_text(name, md[:2_000]),
        cover_url = None,
    )


async def _enrich_cover_bg(cache, isbn: str, file_hash: str) -> None:
    """Busca la portada en Open Library y la persiste en la caché (background).

    No afecta a la respuesta de conversión: si falla o tarda, el Markdown ya
    fue entregado. Solo mejora la portada mostrada en el 'vault'.
    """
    try:
        meta = await _fetch_isbn_meta(isbn)
        cover = meta.get("cover_url") if meta else None
        if cover:
            await asyncio.to_thread(cache.update_cover, file_hash, cover)
    except Exception as exc:
        logger.debug("Enriquecimiento de portada falló (isbn=%s): %s", isbn, exc)


# Pool de procesos (ProcessPoolExecutor) — inicializado en lifespan.
# Cada proceso tiene su propio GIL: paralelismo CPU real en múltiples núcleos.
_convert_pool: concurrent.futures.ProcessPoolExecutor | None = None

# Semáforo global de conversiones: inicializado en lifespan (contexto async)
_convert_sem: asyncio.Semaphore | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Coordinación de workers HTTP (slot assignment + core split)
# ─────────────────────────────────────────────────────────────────────────────

# Lockfile compartido entre workers uvicorn para asignar slots únicos.
# Cada worker abre, lockea, lee N PIDs ya escritos, escribe el suyo y libera.
# Su posición en la lista es su slot (0, 1, 2, …). Robusto ante crashes:
# el archivo se trunca al primer arranque por worker_id=0 si tiene >1h.
_HTTP_SLOT_FILE = Path(os.environ.get("BOOKDORK_RUNTIME_DIR", ".")) / ".bookdork_http_slots"


def _claim_http_slot(n_http: int) -> int:
    """
    Asigna un slot único (0..n_http-1) a este proceso HTTP worker.

    Implementación: archivo .bookdork_http_slots en CWD. Cada worker añade su
    PID atómicamente y recibe como slot su índice de inserción módulo n_http.

    Si solo hay 1 worker (HTTP_WORKERS=1) devuelve 0 sin tocar disco —
    preserva el comportamiento original sin efectos colaterales.

    En caso de error de I/O cae a `os.getpid() % n_http` (peor: dos workers
    podrían coincidir, ligera contención sin caída de servicio).
    """
    if n_http <= 1:
        return 0
    try:
        # Trunca el archivo si tiene > 1h (residuo de un arranque anterior).
        if _HTTP_SLOT_FILE.exists():
            age_s = time.time() - _HTTP_SLOT_FILE.stat().st_mtime
            if age_s > 3600:
                _HTTP_SLOT_FILE.unlink()

        # Append atómico: open en modo 'a' es atómico para writes < PIPE_BUF.
        # En Windows usamos un FileLock cooperativo si está disponible.
        with open(_HTTP_SLOT_FILE, "a+", encoding="utf-8") as f:
            f.seek(0)
            existing = [line.strip() for line in f if line.strip()]
            slot = len(existing) % n_http
            f.write(f"{os.getpid()}\n")
            f.flush()
        return slot
    except OSError as exc:
        logger.warning(
            "No se pudo claim slot HTTP via lockfile (%s). Fallback a PID modulo.", exc,
        )
        return os.getpid() % n_http


def _split_cores(all_cores: list[int], n_http: int, my_slot: int) -> list[int]:
    """
    Reparte `all_cores` entre los n_http workers HTTP. Devuelve el subset
    asignado al slot `my_slot`. Reparto contiguo (no round-robin) para
    preservar locality de caché por worker.

    Ejemplo: all_cores=[2,3,4,5], n_http=2 →
       slot 0 → [2, 3]
       slot 1 → [4, 5]

    Si n_http > len(all_cores), algunos workers reciben el mismo set
    (degradación gradual, no fallo).
    """
    if not all_cores:
        return []
    if n_http <= 1:
        return list(all_cores)
    chunk = max(1, len(all_cores) // n_http)
    start = (my_slot % n_http) * chunk
    end   = start + chunk if my_slot < n_http - 1 else len(all_cores)
    subset = all_cores[start:end]
    return subset if subset else [all_cores[my_slot % len(all_cores)]]


def _worker_affinity_init(counter: multiprocessing.Value, cores: list) -> None:
    """
    Ejecutado UNA vez por proceso worker al crearse.
    Asigna el proceso al núcleo CPU que le corresponde por orden de creación.
    counter es un multiprocessing.Value compartido entre todos los workers.
    """
    with counter.get_lock():
        slot = counter.value
        counter.value += 1
    core = cores[slot % len(cores)]
    try:
        import psutil
        psutil.Process().cpu_affinity([core])
    except Exception:
        pass  # psutil no instalado o plataforma no soporta afinidad de CPU


def _sync_convert(content: bytes, ext: str, filename: str) -> dict:
    """Ejecuta la conversión en un proceso del pool (no bloquea el event loop)."""
    return pdf_engine.convert_document(content, ext, filename)


def _pool_warmup() -> int:
    """Tarea trivial ejecutada en cada proceso del pool durante el pre-warm.

    El simple hecho de ejecutarse fuerza el spawn del proceso y el import de
    `backend.pdf_engine` (con PyMuPDF). Devuelve el PID para trazabilidad.
    No carga torch/easyocr: esos imports siguen diferidos hasta que llegue un
    PDF escaneado real (ver pdf_engine._probe_cuda)."""
    return os.getpid()


async def _read_limited(upload: UploadFile, max_bytes: int) -> bytes:
    """
    Lee un UploadFile en chunks de _CHUNK_SIZE bytes y aborta con 413
    en cuanto el total acumulado supera max_bytes.

    Esto evita que el servidor cargue archivos enormes en RAM antes de
    comprobar el tamaño (el cheque sucede incremental, en streaming).
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"'{upload.filename}' supera el límite de "
                    f"{max_bytes // (1024 * 1024)} MB."
                ),
            )
        chunks.append(chunk)
    return b"".join(chunks)

# ─────────────────────────────────────────────────────────────────────────────
# Aplicación FastAPI
# ─────────────────────────────────────────────────────────────────────────────
settings = get_settings()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Motor de búsqueda de libros basado en Google Dorks y Meilisearch",
    docs_url="/api/docs" if settings.DEBUG else None,   # Ocultar Swagger en producción
    redoc_url="/api/redoc" if settings.DEBUG else None,
    openapi_url="/api/openapi.json" if settings.DEBUG else None,
    lifespan=lifespan,
)

# ── Middlewares (orden: último en declararse = primero en ejecutarse) ──────────

# 1. Cabeceras de seguridad HTTP
app.add_middleware(SecurityHeadersMiddleware)

# 2. Límite de velocidad por IP
app.add_middleware(RateLimitMiddleware)

# 3. CORS — solo los orígenes permitidos en config
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,                # No cookies de sesión
    allow_methods=["GET", "POST"],          # Solo métodos necesarios
    allow_headers=["Content-Type", "X-Admin-API-Key", "Authorization"],
    max_age=3600,
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(admin_router)

# ── Frontend estático ──────────────────────────────────────────────────────────
# Solo se sirven extensiones públicas conocidas. Cualquier otro archivo
# (Dockerfile, requirements.txt, .zip, .env, .bak, .map, etc.) devuelve 404
# aunque por error termine dentro del directorio Frontend/.
class SafeStaticFiles(StaticFiles):
    _ALLOWED_EXTS = frozenset({
        ".html", ".css", ".js", ".mjs",
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico",
        ".woff", ".woff2", ".ttf", ".otf",
    })

    async def get_response(self, path: str, scope):  # type: ignore[override]
        if not path or path.endswith("/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        ext = os.path.splitext(path)[1].lower()
        if not ext or ext not in self._ALLOWED_EXTS:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return await super().get_response(path, scope)


# Si existe Frontend/dist/ (salida de build_assets.py), se sirve esa versión
# con assets minificados y hasheados; si no, fallback a Frontend/ fuente para
# desarrollo local sin paso de build.
_FRONTEND_ROOT = Path(__file__).parent.parent / "Frontend"
_FRONTEND_DIST = _FRONTEND_ROOT / "dist"
FRONTEND_DIR   = _FRONTEND_DIST if _FRONTEND_DIST.exists() else _FRONTEND_ROOT
if FRONTEND_DIR.exists():
    app.mount(
        "/static",
        SafeStaticFiles(directory=str(FRONTEND_DIR)),
        name="static",
    )

# ─────────────────────────────────────────────────────────────────────────────
# Dependencias reutilizables
# ─────────────────────────────────────────────────────────────────────────────
dork_engine = BookDorkEngine()


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints públicos
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def serve_landing():
    """Sirve la landing page pública."""
    page = FRONTEND_DIR / "landing.html"
    if page.exists():
        return FileResponse(str(page))
    return JSONResponse({"detail": "Página no encontrada"}, status_code=404)


@app.get("/search", include_in_schema=False)
async def serve_frontend():
    """Sirve el buscador de libros (requiere autenticación en el cliente)."""
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return JSONResponse(
        {"detail": "Frontend no encontrado. Asegúrate de que existe frontend/index.html"},
        status_code=404,
    )


@app.get(
    "/api/health",
    response_model=HealthResponse,
    tags=["Sistema"],
    summary="Estado del sistema",
)
async def health_check():
    """
    Verifica el estado de todos los componentes del sistema.
    No requiere autenticación. Seguro para monitorización externa.
    """
    meili = get_meili_client()
    return HealthResponse(
        status="ok" if meili.is_healthy() else "degraded",
        version=settings.APP_VERSION,
        meilisearch_connected=meili.is_healthy(),
        uptime_seconds=time.monotonic() - _app_start_time,
    )


@app.get(
    "/api/search",
    response_model=SearchResponse,
    tags=["Búsqueda"],
    summary="Busca libros usando Google Dorks y Meilisearch",
)
async def search_books(
    request: Request,
    q: str = Query(..., min_length=2, max_length=512, description="Consulta de búsqueda"),
    filetype: Optional[str] = Query(default="any", description="Tipo de archivo"),
    site: Optional[str] = Query(default="any", description="Sitio de origen"),
    language: Optional[str] = Query(default="any", description="Idioma del libro"),
    author: Optional[str] = Query(default=None, max_length=150, description="Autor"),
    isbn: Optional[str] = Query(default=None, max_length=20, description="ISBN"),
    page: int = Query(default=1, ge=1, le=100),
    limit: int = Query(default=20, ge=1, le=50),
):
    """
    Endpoint principal de búsqueda de libros.

    Flujo:
    1. Valida y sanitiza los parámetros de entrada
    2. Ejecuta búsqueda en Meilisearch (typo tolerance, fuzzy matching)
    3. Genera la consulta Google Dork correspondiente
    4. Combina resultados y los devuelve al frontend

    Los resultados locales (Meilisearch) incluyen la URL Dork para cada libro.
    La URL Dork principal permite al usuario buscar el libro en Google directamente.
    """
    # Validar User-Agent básico
    check_user_agent(request)

    # Construir el objeto de solicitud validado (Pydantic sanitiza automáticamente)
    try:
        search_req = SearchRequest(
            query=q,
            filetype=filetype or "any",
            site=site or "any",
            language=language or "any",
            author=author,
            isbn=isbn,
            page=page,
            limit=limit,
        )
    except Exception as exc:
        logger.warning("SearchRequest validation error: %s", exc)
        raise HTTPException(status_code=422, detail="Parámetros de búsqueda inválidos.") from exc

    t_start = time.monotonic()

    # ── 1. Búsqueda en Meilisearch ────────────────────────────────────────────
    meili = get_meili_client()
    meili_result = meili.search(search_req)
    hits = meili_result.get("hits", [])
    total = meili_result.get("estimatedTotalHits", 0)
    meili_ms = meili_result.get("processingTimeMs", 0)

    # ── 2. Generar consulta Dork principal ────────────────────────────────────
    primary_dork = dork_engine.build_primary(
        query=search_req.query,
        filetype=search_req.filetype if search_req.filetype != "any" else None,
        site=search_req.site if search_req.site != "any" else None,
        author=search_req.author,
        isbn=search_req.isbn,
    )

    # ── 3. Mapear resultados de Meilisearch a BookResult ──────────────────────
    results: list[BookResult] = []
    for hit in hits:
        # Generar URL Dork específica para este libro
        book_dork = dork_engine.strategy_direct_pdf(
            query=hit.get("title", search_req.query),
            author=hit.get("author"),
            filetype=hit.get("filetype"),
        )
        results.append(
            BookResult(
                id=hit.get("id", ""),
                title=hit.get("title", "Sin título"),
                author=hit.get("author"),
                description=hit.get("_formatted", {}).get("description", hit.get("description")),
                filetype=hit.get("filetype"),
                language=hit.get("language"),
                year=hit.get("year"),
                source_site=hit.get("source_site"),
                isbn=hit.get("isbn"),
                dork_url=book_dork.google_url,
                tags=hit.get("tags", []),
            )
        )

    # ── 4. Sugerencias ortográficas ───────────────────────────────────────────
    suggestions = []
    if not results:
        suggestions = meili.get_suggestions(search_req.query)

    total_ms = int((time.monotonic() - t_start) * 1000)

    return SearchResponse(
        query=search_req.query,
        dork_query=primary_dork.raw_query,
        dork_url=primary_dork.google_url,
        total_hits=total,
        page=page,
        limit=limit,
        processing_time_ms=total_ms + meili_ms,
        results=results,
        suggestions=suggestions,
    )


@app.get(
    "/api/search/external",
    tags=["Búsqueda"],
    summary="Proxy de Open Library — evita problemas CORS en el navegador",
)
async def search_external(
    request: Request,
    q: str = Query(..., min_length=2, max_length=512),
    limit: int = Query(default=15, ge=1, le=20),
):
    """
    Hace la petición a Open Library desde el servidor (sin CORS).
    Devuelve la misma estructura que openlibrary.org/search.json
    para que el frontend la procese igual que antes.
    """
    check_user_agent(request)

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://openlibrary.org/search.json",
                params={
                    "q":      q,
                    "limit":  limit,
                    "fields": "key,title,author_name,first_publish_year,isbn,language,cover_i,subject",
                },
                headers={"Accept": "application/json", "User-Agent": "BookDork/1.0"},
            )
            resp.raise_for_status()
            return JSONResponse(resp.json())
    except Exception as exc:
        logger.warning("Open Library proxy falló: %s", exc)
        return JSONResponse({"docs": [], "numFound": 0})


@app.get(
    "/api/dork",
    tags=["Búsqueda"],
    summary="Genera todas las variantes de consulta Dork sin ejecutar búsqueda",
)
async def get_dork_queries(
    request: Request,
    q: str = Query(..., min_length=2, max_length=512),
    filetype: Optional[str] = Query(default=None),
    site: Optional[str] = Query(default=None),
    author: Optional[str] = Query(default=None, max_length=150),
    isbn: Optional[str] = Query(default=None, max_length=20),
):
    """
    Devuelve todas las variantes de consulta Google Dork para una búsqueda.
    Útil para que el usuario elija la estrategia de búsqueda que prefiera.
    """
    check_user_agent(request)

    all_dorks = dork_engine.build_all(
        query=q,
        filetype=filetype,
        site=site,
        author=author,
        isbn=isbn,
    )

    return {
        "query": q,
        "dork_variants": [
            {
                "strategy": d.strategy,
                "description": d.description,
                "raw_query": d.raw_query,
                "google_url": d.google_url,
                "operators_used": d.operators_used,
            }
            for d in all_dorks
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints de administración (requieren API Key)
# ─────────────────────────────────────────────────────────────────────────────

@app.post(
    "/api/index/book",
    tags=["Administración"],
    summary="Indexa un libro en Meilisearch",
    status_code=201,
)
async def index_book(
    request: Request,
    book: IndexBookRequest,
    principal: AdminPrincipal = Depends(require_admin_access(Role.SUPERADMIN)),
):
    """
    Añade un libro al índice local de Meilisearch.
    Requiere la cabecera 'X-Admin-API-Key' con la clave de administrador.
    """
    check_user_agent(request)
    meili = get_meili_client()
    book_dict = book.model_dump()
    success = meili.index_book(book_dict)

    if not success:
        raise HTTPException(
            status_code=503,
            detail="Error al indexar el libro. Verifica que Meilisearch está disponible.",
        )
    logger.info(
        "AUDIT index_book — title='%s' author='%s' actor=%s ip=%s",
        book.title,
        book.author or "N/A",
        principal.audit_label,
        request.client.host if request.client else "unknown",
    )
    return {"message": "Libro indexado correctamente.", "title": book.title}


@app.get(
    "/api/index/stats",
    tags=["Administración"],
    summary="Estadísticas del índice de libros",
)
async def get_index_stats(
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_access(Role.SUPERADMIN)),
):
    """
    Devuelve estadísticas del índice: número de documentos, último update, etc.
    """
    check_user_agent(request)
    logger.info(
        "AUDIT index_stats — actor=%s ip=%s",
        principal.audit_label,
        request.client.host if request.client else "unknown",
    )
    meili = get_meili_client()
    return {"stats": meili.get_stats()}


@app.post(
    "/api/cache/reclassify",
    tags=["Administración"],
    summary="Reclasifica todos los libros del caché con el clasificador actualizado",
)
async def reclassify_cache(
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_access(Role.SUPERADMIN)),
):
    """
    Recorre cada entrada del caché, vuelve a clasificar el tema con el algoritmo
    actual y actualiza 'topic' + 'book_id' en la base de datos SQLite.
    """
    check_user_agent(request)
    settings = get_settings()
    if not settings.CACHE_ENABLED:
        raise HTTPException(status_code=503, detail="Caché desactivado.")
    cache = get_cache(settings.CACHE_DIR)
    result = await asyncio.to_thread(cache.reclassify_all)
    logger.info("AUDIT reclassify_cache — checked=%d updated=%d actor=%s ip=%s",
                result["checked"], result["updated"], principal.audit_label,
                request.client.host if request.client else "unknown")
    return result


@app.post(
    "/api/cache/reextract-metadata",
    tags=["Administración"],
    summary="Re-extrae title y author faltantes en el caché",
)
async def reextract_metadata(
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_access(Role.SUPERADMIN)),
):
    """
    Recorre las entradas con title o author vacíos y los re-extrae
    del markdown y del nombre de archivo con el extractor actualizado.
    """
    check_user_agent(request)
    settings = get_settings()
    if not settings.CACHE_ENABLED:
        raise HTTPException(status_code=503, detail="Caché desactivado.")
    cache = get_cache(settings.CACHE_DIR)
    result = await asyncio.to_thread(cache.reextract_metadata_all)
    logger.info("AUDIT reextract_metadata — checked=%d updated=%d actor=%s ip=%s",
                result["checked"], result["updated"], principal.audit_label,
                request.client.host if request.client else "unknown")
    return result


@app.post(
    "/api/cache/backfill-covers",
    tags=["Administración"],
    summary="Rellena cover_url para libros con ISBN en el caché",
)
async def backfill_covers(
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_access(Role.SUPERADMIN)),
):
    """
    Para cada entrada con ISBN y sin cover_url, consulta Open Library
    y almacena la URL de portada. Útil para libros ya convertidos.
    """
    check_user_agent(request)
    settings = get_settings()
    if not settings.CACHE_ENABLED:
        raise HTTPException(status_code=503, detail="Caché desactivado.")

    cache = get_cache(settings.CACHE_DIR)

    with cache._lock:
        rows = cache._db.execute(
            "SELECT file_hash, isbn FROM conversions "
            "WHERE isbn IS NOT NULL AND isbn != '' "
            "AND (cover_url IS NULL OR cover_url = '')"
        ).fetchall()

    checked = len(rows)
    updated = 0
    for file_hash, isbn in rows:
        meta = await _fetch_isbn_meta(isbn)
        cover = meta.get("cover_url", "")
        if cover:
            with cache._lock:
                cache._db.execute(
                    "UPDATE conversions SET cover_url = ? WHERE file_hash = ?",
                    (cover, file_hash),
                )
                cache._db.commit()
            updated += 1

    logger.info("AUDIT backfill_covers — checked=%d updated=%d actor=%s ip=%s",
                checked, updated, principal.audit_label,
                request.client.host if request.client else "unknown")
    return {"checked": checked, "updated": updated}


# ─────────────────────────────────────────────────────────────────────────────
# Convertidor Markdown
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/auth", include_in_schema=False)
async def serve_auth():
    """Sirve la página de autenticación."""
    page = FRONTEND_DIR / "auth.html"
    if page.exists():
        return FileResponse(str(page))
    return JSONResponse({"detail": "Página no encontrada"}, status_code=404)


@app.get("/vault", include_in_schema=False)
async def serve_vault():
    """Sirve The Info Vault."""
    page = FRONTEND_DIR / "vault.html"
    if page.exists():
        return FileResponse(str(page))
    return JSONResponse({"detail": "Página no encontrada"}, status_code=404)


@app.get("/converter", include_in_schema=False)
async def serve_converter():
    """Sirve la página del convertidor Markdown."""
    page = FRONTEND_DIR / "converter.html"
    if page.exists():
        return FileResponse(str(page))
    return JSONResponse({"detail": "Página no encontrada"}, status_code=404)


@app.get("/plans", include_in_schema=False)
async def serve_plans():
    """Sirve la página de planes y precios."""
    page = FRONTEND_DIR / "plans.html"
    if page.exists():
        return FileResponse(str(page))
    return JSONResponse({"detail": "Página no encontrada"}, status_code=404)


@app.get("/checkout", include_in_schema=False)
async def serve_checkout():
    """Sirve la página de pago (simulación de checkout)."""
    page = FRONTEND_DIR / "checkout.html"
    if page.exists():
        return FileResponse(str(page))
    return JSONResponse({"detail": "Página no encontrada"}, status_code=404)


@app.get("/legal", include_in_schema=False)
async def serve_legal():
    """Sirve el aviso legal."""
    page = FRONTEND_DIR / "legal.html"
    if page.exists():
        return FileResponse(str(page))
    return JSONResponse({"detail": "Página no encontrada"}, status_code=404)


@app.get(
    "/api/vault/books",
    tags=["The Info Vault"],
    summary="Lista los libros más descargados del Vault",
)
async def get_vault_books(
    request: Request,
    topic:   Optional[str] = Query(default=None, description="Categoría temática"),
    year:    Optional[int] = Query(default=None, description="Año de publicación"),
    edition: Optional[str] = Query(default=None, description="Edición (usa 'na' para desconocida)"),
    period:  Optional[str] = Query(default=None, description="today|week|biweek|month"),
    limit:   int           = Query(default=24, ge=1, le=100),
    offset:  int           = Query(default=0, ge=0),
    auth:    AuthContext   = Depends(get_auth_context),
):
    """
    Devuelve la lista de libros convertidos ordenados por descargas.
    Requiere plan Basic o Pro.
    """
    check_user_agent(request)

    from .firebase_admin_client import get_firestore_user
    try:
        user_data = await get_firestore_user(auth.uid)
        plan = user_data.get("plan") or "gratis"
    except Exception:
        plan = "gratis"

    if plan not in ("basic", "pro"):
        raise HTTPException(
            status_code=403,
            detail={
                "error":   "vault_access_denied",
                "message": "The Info Vault requiere un plan Basic o Pro.",
                "plan":    plan,
            },
        )

    if not settings.CACHE_ENABLED:
        return {"total": 0, "books": []}

    cache = get_cache(settings.CACHE_DIR)
    return cache.list_books(
        topic=topic, year=year, edition=edition,
        period=period, limit=limit, offset=offset,
    )


@app.get(
    "/api/vault/download/{book_id}",
    tags=["The Info Vault"],
    summary="Descarga el Markdown de un libro del Vault",
)
async def download_vault_book(
    request: Request,
    book_id: str,
    auth:    AuthContext = Depends(get_auth_context),
):
    """
    Devuelve el contenido Markdown de un libro convertido e incrementa
    su contador de descargas. Requiere plan Basic o Pro.
    """
    check_user_agent(request)

    from .firebase_admin_client import get_firestore_user
    try:
        user_data = await get_firestore_user(auth.uid)
        plan = user_data.get("plan") or "gratis"
    except Exception:
        plan = "gratis"

    if plan not in ("basic", "pro"):
        raise HTTPException(
            status_code=403,
            detail={"error": "vault_access_denied", "plan": plan},
        )

    if not settings.CACHE_ENABLED:
        raise HTTPException(status_code=503, detail="Caché desactivado.")

    # Validar formato del book_id antes de consultar la base de datos
    if not re.match(r'^[\w\-]{1,80}$', book_id):
        raise HTTPException(status_code=400, detail="Identificador de libro inválido.")

    cache  = get_cache(settings.CACHE_DIR)
    result = cache.get_by_book_id(book_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Libro no encontrado en el Vault.")

    markdown, filename = result
    cache.increment_download(book_id)

    # Sanitización estricta: solo alfanumérico, guión y guión bajo; sin puntos (path traversal)
    stem = Path(filename).stem if filename else book_id
    safe = re.sub(r'[^\w\-]', '_', stem).strip('_')[:100] or "book"

    return JSONResponse(
        content={"markdown": markdown, "filename": f"{safe}.md"},
        headers={"X-Book-Id": book_id},
    )


@app.get(
    "/api/book-meta",
    tags=["Conversión"],
    summary="Metadatos de un libro por ISBN (Open Library), carga diferida",
)
async def get_book_meta(
    isbn: str = Query(..., min_length=10, max_length=17),
    auth: AuthContext = Depends(get_auth_context),
):
    """Devuelve metadatos de Open Library para un ISBN.

    Servido aparte de /api/convert para que la conversión no espere a un tercero
    (ver evaluación de latencia). El frontend lo invoca de forma diferida tras
    renderizar cada resultado. Requiere autenticación para evitar uso como proxy
    abierto. Siempre responde 200 con `{}` si el ISBN no se encuentra.
    """
    normalized = re.sub(r'[^0-9Xx]', '', isbn).upper()
    if len(normalized) not in (10, 13):
        raise HTTPException(status_code=400, detail="ISBN inválido.")
    meta = await _fetch_isbn_meta(normalized)
    return JSONResponse(content=meta or {})


@app.post(
    "/api/convert",
    tags=["Conversión"],
    summary="Convierte hasta 5 archivos de libro a Markdown en paralelo",
)
async def convert_books_to_markdown(
    request: Request,
    files: List[UploadFile] = File(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Acepta 1–5 archivos (PDF, EPUB, MOBI, AZW3, DJVU, TXT) y los convierte
    a Markdown en paralelo usando un ThreadPoolExecutor protegido por semáforo.

    Seguridad:
      • Requiere Firebase ID token válido (Authorization: Bearer <token>).
      • Verifica y decrementa el límite de conversiones server-side vía Firestore.
      • Lee archivos en streaming (256 KB chunks) para evitar DoS por RAM.
      • Timeout por archivo para prevenir agotamiento del ThreadPool.
      • Semáforo global limita conversiones concurrentes en todo el servidor.
    """
    check_user_agent(request)
    _t_start = time.perf_counter()   # instrumentación de latencia por etapa

    if not pdf_engine.is_available():
        raise HTTPException(
            status_code=503,
            detail=(
                "Servicio de conversión no disponible. "
                "Ejecuta: pip install pymupdf  # o: pip install 'markitdown[pdf]'"
            ),
        )

    if not files:
        raise HTTPException(status_code=400, detail="No se recibió ningún archivo.")

    if len(files) > _MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Máximo {_MAX_FILES} archivos por solicitud.",
        )

    # ── Separar archivos válidos de inválidos antes de leer contenido ─────────
    valid_uploads: list[tuple[UploadFile, str]] = []
    rejected: list[dict] = []
    for upload in files:
        name = upload.filename or "archivo"
        _, ext = os.path.splitext(name.lower())
        if ext in _ALLOWED_BOOK_EXTS:
            valid_uploads.append((upload, ext))
        else:
            rejected.append({
                "success":           False,
                "original_filename": name,
                "error":             f"Formato '{ext or 'desconocido'}' no permitido.",
            })

    if not valid_uploads:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Ningún archivo tiene formato válido. "
                f"Permitidos: {', '.join(sorted(_ALLOWED_BOOK_EXTS))}"
            ),
        )

    # ── Límite de tamaño según plan del usuario ───────────────────────────────
    _plan_lim      = await get_user_plan_limits(auth.uid)
    _max_file_bytes = _plan_lim["max_file_bytes"]

    # ── Leer todos los archivos en paralelo con streaming (evita DoS de RAM) ──
    async def _safe_read(upload: UploadFile, ext: str) -> dict | tuple:
        """Lee en chunks; retorna tupla (bytes, ext, name, sha256) o dict de error."""
        name = upload.filename or "archivo"
        try:
            content = await _read_limited(upload, _max_file_bytes)
            file_hash = ConversionCache.hash_content(content)
            return (content, ext, name, file_hash)
        except HTTPException as exc:
            return {"success": False, "original_filename": name, "error": exc.detail}

    read_results = await asyncio.gather(
        *[_safe_read(u, e) for u, e in valid_uploads]
    )
    _t_read = time.perf_counter()

    # ── Separar cache hits de misses ──────────────────────────────────────────
    cache = get_cache(settings.CACHE_DIR) if settings.CACHE_ENABLED else None
    final_results: list[Optional[dict]] = [None] * len(read_results)
    # (orig_idx, content, ext, name, file_hash)
    need_convert: list[tuple[int, bytes, str, str, str]] = []

    for i, item in enumerate(read_results):
        if isinstance(item, dict):              # Error de lectura
            final_results[i] = item
            continue
        content, ext, name, file_hash = item
        if cache:
            cached = cache.get(file_hash)
            if cached:
                final_results[i] = cached
                continue
        need_convert.append((i, content, ext, name, file_hash))

    # ── Reservar slots sólo para archivos que requieren conversión ────────────
    if need_convert:
        slots = await check_and_reserve(uid=auth.uid, num_files=len(need_convert))
        need_convert = need_convert[:slots]
    else:
        slots = 0

    # ── Convertir en paralelo: semáforo global + timeout por archivo ──────────
    loop = asyncio.get_running_loop()

    async def _dispatch(
        orig_idx: int, content: bytes, ext: str, name: str, file_hash: str
    ) -> tuple[int, bytes, str, str, dict]:
        if _convert_sem is None or _convert_pool is None:
            raise RuntimeError(
                "El servidor no está listo: semáforo o pool de conversión no inicializados."
            )
        async with _convert_sem:
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        _convert_pool, _sync_convert, content, ext, name
                    ),
                    timeout=settings.CONVERT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                result = {
                    "success":           False,
                    "original_filename": name,
                    "error": (
                        f"Tiempo de procesamiento excedido "
                        f"({settings.CONVERT_TIMEOUT_SECONDS}s). "
                        "El archivo puede ser demasiado complejo o estar dañado."
                    ),
                }
        return orig_idx, content, file_hash, name, result

    conversion_pairs = await asyncio.gather(
        *[_dispatch(i, c, e, n, h) for (i, c, e, n, h) in need_convert]
    )
    _t_convert = time.perf_counter()

    # ── Persistir en caché y devolver el Markdown SIN esperar a Open Library ──
    # El fetch de metadatos ISBN solía estar en la ruta crítica: una llamada
    # HTTP externa (hasta ISBN_LOOKUP_TIMEOUT_S por archivo) que podía añadir
    # varios segundos a una conversión ya terminada. Ahora:
    #   • Devolvemos `isbn` en cada resultado → el frontend pide los metadatos de
    #     forma diferida a /api/book-meta (no bloquea el Markdown).
    #   • La portada se enriquece en background y se guarda en la caché para que
    #     el "vault" la muestre sin coste en la ruta crítica.
    #   • La persistencia (regex CPU-bound + sqlite) corre en un hilo para no
    #     bloquear el event loop.
    async def _enrich(orig_idx: int, content: bytes, file_hash: str,
                      name: str, result: dict):
        if not result.get("success"):
            return orig_idx, result
        md       = result["markdown"]
        isbn_val = extract_isbn(md[:5_000])
        result["isbn"] = isbn_val   # consumido por el panel de metadatos diferido
        if cache:
            book_id = await asyncio.to_thread(
                _persist_conversion, cache, file_hash, md, name,
                len(content), isbn_val, result.get("engine_used"),
            )
            result["book_id"] = book_id
            # Enriquecimiento de portada fire-and-forget (no bloquea la respuesta)
            if isbn_val:
                _spawn_bg(_enrich_cover_bg(cache, isbn_val, file_hash))
        return orig_idx, result

    # Archivos con error ya están en final_results; solo enriquecer los OK
    enrich_results = await asyncio.gather(*[
        _enrich(orig_idx, content, file_hash, name, result)
        for orig_idx, content, file_hash, name, result in conversion_pairs
    ])
    for orig_idx, result in enrich_results:
        final_results[orig_idx] = result
    _t_enrich = time.perf_counter()

    # Combinar: conversiones reales + archivos rechazados por formato
    conversion_results = [r for r in final_results if r is not None]
    all_results = conversion_results + rejected
    success_count    = sum(1 for r in all_results if r.get("success"))
    cache_hit_count  = sum(1 for r in conversion_results if r.get("cache_hit"))

    logger.info(
        "Conversion done — uid=%s %d/%d OK  cache_hits=%d  slots_used=%d  "
        "timing(s): read=%.2f convert=%.2f persist=%.2f total=%.2f",
        auth.uid[:8], success_count, len(all_results), cache_hit_count, slots,
        _t_read - _t_start, _t_convert - _t_read,
        _t_enrich - _t_convert, _t_enrich - _t_start,
    )

    return {
        "total":           len(all_results),
        "success_count":   success_count,
        "cache_hit_count": cache_hit_count,
        "results":         all_results,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Manejo de errores global
# ─────────────────────────────────────────────────────────────────────────────

@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return JSONResponse(
        {"detail": "Recurso no encontrado"},
        status_code=404,
    )


@app.exception_handler(500)
async def internal_error_handler(request: Request, exc):
    logger.exception("Error interno no controlado: %s", exc)
    return JSONResponse(
        {"detail": "Error interno del servidor"},
        status_code=500,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Punto de entrada — Ejecuta Hypercorn con HTTP/3
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Lanza el servidor con Hypercorn para soporte HTTP/3 (QUIC).

    Hypercorn es un servidor ASGI que implementa HTTP/1.1, HTTP/2 y HTTP/3.
    Para HTTP/3 es OBLIGATORIO TLS — sin certificado, no hay QUIC.

    Generación de certificado autofirmado para desarrollo:
        mkdir -p certs
        openssl req -x509 -newkey rsa:4096 -keyout certs/server.key \\
                -out certs/server.crt -days 365 -nodes \\
                -subj "/CN=localhost"
    """
    import hypercorn.asyncio
    from hypercorn.config import Config as HypercornConfig

    cfg = HypercornConfig()
    cfg.bind = [f"{settings.HOST}:{settings.HTTP3_PORT}"]

    cert = Path(settings.TLS_CERT_FILE)
    key = Path(settings.TLS_KEY_FILE)
    if not settings.FORCE_HTTP and cert.exists() and key.exists():
        cfg.certfile = str(cert)
        cfg.keyfile = str(key)
        cfg.alpn_protocols = ["h3", "h2", "http/1.1"]
        cfg.quic_bind = [f"{settings.HOST}:{settings.HTTP3_PORT}"]
        logger.info("TLS habilitado — HTTP/3 activo en :%d", settings.HTTP3_PORT)
    else:
        logger.warning(
            "HTTP/1.1 sin TLS en :%d (FORCE_HTTP=%s)",
            settings.HTTP3_PORT, settings.FORCE_HTTP,
        )

    cfg.accesslog = "-"
    cfg.errorlog = "-"
    cfg.loglevel = "INFO"

    # Multi-worker: honra HTTP_WORKERS también por esta vía (entrypoint de Docker
    # `python -m backend.main`). Antes solo start.py lo aplicaba, así que el
    # contenedor corría siempre 1 worker pese a la config. Cada worker reparte
    # CONVERT_MAX_CONCURRENT y toma su slot de cores en el lifespan.
    cfg.workers = max(1, settings.HTTP_WORKERS)
    if cfg.workers > 1:
        logger.info(
            "Multi-worker: %d procesos HTTP (cores %s, conversión en %s).",
            cfg.workers, settings.HTTP_CPU_CORES, settings.CONVERT_CPU_CORES,
        )
        # Limpia el lockfile de slots de arranques previos (evita stale entries).
        _slot_file = Path(os.environ.get("BOOKDORK_RUNTIME_DIR", ".")) / ".bookdork_http_slots"
        try:
            _slot_file.unlink()
        except OSError:
            pass

    asyncio.run(hypercorn.asyncio.serve(app, cfg))
