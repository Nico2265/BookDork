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
from .firebase_auth import AuthContext, check_and_reserve, get_auth_context, get_user_plan_limits
from .firebase_admin_client import initialize_admin_sdk
from .admin_routes import router as admin_router
from .security import (
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    check_user_agent,
    require_admin_key,
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

    # Inicializar semáforo global de conversiones (debe crearse en contexto async)
    global _convert_sem, _convert_pool
    _convert_sem = asyncio.Semaphore(settings.CONVERT_MAX_CONCURRENT)
    logger.info("✓ Semáforo de conversiones: máx. %d simultáneas.", settings.CONVERT_MAX_CONCURRENT)

    # Pool de procesos con afinidad CPU: cada worker se fija a un núcleo distinto.
    # ProcessPoolExecutor crea procesos separados → cada uno tiene su propio GIL
    # → paralelismo CPU real (no hay contención entre workers de conversión).
    _worker_counter = multiprocessing.Value("i", 0)
    _convert_pool = concurrent.futures.ProcessPoolExecutor(
        max_workers=len(settings.CONVERT_CPU_CORES),
        initializer=_worker_affinity_init,
        initargs=(_worker_counter, list(settings.CONVERT_CPU_CORES)),
    )
    logger.info(
        "✓ Pool de conversión: %d procesos worker en núcleos CPU %s.",
        len(settings.CONVERT_CPU_CORES), settings.CONVERT_CPU_CORES,
    )

    # Fijar el event loop asyncio a los núcleos 0-1 para que no compita con workers.
    try:
        import psutil as _ps
        _ps.Process().cpu_affinity([0, 1])
        logger.info("✓ Event loop asyncio fijado a núcleos 0-1.")
    except Exception:
        pass

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

    # Estado de los motores de conversión
    eng = pdf_engine.get_engine_status()
    logger.info(
        "✓ Motores activos — PyMuPDF: %s | CUDA/OCR: %s | MarkItDown: %s",
        eng["pymupdf"], eng["cuda"], eng["markitdown"],
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
        async with httpx.AsyncClient(timeout=8.0) as client:
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

# Pool de procesos (ProcessPoolExecutor) — inicializado en lifespan.
# Cada proceso tiene su propio GIL: paralelismo CPU real en múltiples núcleos.
_convert_pool: concurrent.futures.ProcessPoolExecutor | None = None

# Semáforo global de conversiones: inicializado en lifespan (contexto async)
_convert_sem: asyncio.Semaphore | None = None


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
# Sirve los archivos HTML/CSS/JS del directorio frontend/
FRONTEND_DIR = Path(__file__).parent.parent / "Frontend"
if FRONTEND_DIR.exists():
    app.mount(
        "/static",
        StaticFiles(directory=str(FRONTEND_DIR)),
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
    _: str = Depends(require_admin_key),
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
        "AUDIT index_book — title='%s' author='%s' ip=%s",
        book.title,
        book.author or "N/A",
        request.client.host if request.client else "unknown",
    )
    return {"message": "Libro indexado correctamente.", "title": book.title}


@app.get(
    "/api/index/stats",
    tags=["Administración"],
    summary="Estadísticas del índice de libros",
)
async def get_index_stats(request: Request, _: str = Depends(require_admin_key)):
    """
    Devuelve estadísticas del índice: número de documentos, último update, etc.
    """
    check_user_agent(request)
    logger.info(
        "AUDIT index_stats — ip=%s",
        request.client.host if request.client else "unknown",
    )
    meili = get_meili_client()
    return {"stats": meili.get_stats()}


@app.post(
    "/api/cache/reclassify",
    tags=["Administración"],
    summary="Reclasifica todos los libros del caché con el clasificador actualizado",
)
async def reclassify_cache(request: Request, _: str = Depends(require_admin_key)):
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
    logger.info("AUDIT reclassify_cache — checked=%d updated=%d ip=%s",
                result["checked"], result["updated"],
                request.client.host if request.client else "unknown")
    return result


@app.post(
    "/api/cache/reextract-metadata",
    tags=["Administración"],
    summary="Re-extrae title y author faltantes en el caché",
)
async def reextract_metadata(request: Request, _: str = Depends(require_admin_key)):
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
    logger.info("AUDIT reextract_metadata — checked=%d updated=%d ip=%s",
                result["checked"], result["updated"],
                request.client.host if request.client else "unknown")
    return result


@app.post(
    "/api/cache/backfill-covers",
    tags=["Administración"],
    summary="Rellena cover_url para libros con ISBN en el caché",
)
async def backfill_covers(request: Request, _: str = Depends(require_admin_key)):
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

    logger.info("AUDIT backfill_covers — checked=%d updated=%d ip=%s",
                checked, updated,
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

    # ── Guardar en caché, enriquecer con Open Library y rellenar resultados ──
    isbn_meta_tasks = {}
    for orig_idx, content, file_hash, name, result in conversion_pairs:
        if result.get("success"):
            md   = result["markdown"]
            isbn = extract_isbn(md[:5_000])
            if isbn:
                isbn_meta_tasks[orig_idx] = (isbn, content, file_hash, name, result)
            else:
                isbn_meta_tasks[orig_idx] = (None, content, file_hash, name, result)

    # Buscar metadatos ISBN en paralelo para todos los archivos que lo tengan
    async def _enrich(orig_idx: int, isbn, content, file_hash, name, result):
        if not result.get("success"):
            return orig_idx, result
        md       = result["markdown"]
        isbn_val = isbn
        meta     = await _fetch_isbn_meta(isbn_val) if isbn_val else {}
        if cache:
            book_id = cache.put(
                file_hash = file_hash,
                markdown  = md,
                filename  = name,
                file_size = len(content),
                isbn      = isbn_val,
                language  = detect_language(md),
                topic     = classify_topic(name, md),
                engine    = result.get("engine_used"),
                title     = clean_title(name),
                author    = extract_author_from_text(name, md[:6_000]),
                year      = extract_year_from_text(name, md[:6_000]),
                edition   = extract_edition_from_text(name, md[:2_000]),
                cover_url = meta.get("cover_url") if meta else None,
            )
            result["book_id"] = book_id
        if meta:
            result["book_meta"] = meta
        return orig_idx, result

    # Archivos con error ya están en final_results; solo enriquecer los OK
    enrich_results = await asyncio.gather(*[
        _enrich(i, *vals)
        for i, vals in isbn_meta_tasks.items()
    ])
    for orig_idx, result in enrich_results:
        final_results[orig_idx] = result

    # Combinar: conversiones reales + archivos rechazados por formato
    conversion_results = [r for r in final_results if r is not None]
    all_results = conversion_results + rejected
    success_count    = sum(1 for r in all_results if r.get("success"))
    cache_hit_count  = sum(1 for r in conversion_results if r.get("cache_hit"))

    logger.info(
        "Conversion done — uid=%s %d/%d OK  cache_hits=%d  slots_used=%d",
        auth.uid[:8], success_count, len(all_results), cache_hit_count, slots,
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

    asyncio.run(hypercorn.asyncio.serve(app, cfg))
