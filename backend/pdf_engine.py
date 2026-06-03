"""
=============================================================================
pdf_engine.py — Motor multi-ruta de extracción de documentos a Markdown
=============================================================================
Rutas de conversión en orden de prioridad:

  1. PyMuPDF (fitz)          PDFs con texto incrustado — 10-50× más rápido
                              que pdfminer. C puro, sin GIL significativo.

  2. EasyOCR + CUDA          PDFs escaneados (páginas imagen). Usa la GPU
                              NVIDIA RTX 3060 Ti (8 GB GDDR6) si torch+CUDA
                              están instalados; cae a CPU automáticamente.

  3. MarkItDown (fallback)   Compatibilidad máxima: EPUB, MOBI, AZW3, DjVu,
                              TXT y cualquier PDF no manejado arriba.

Detección de PDF escaneado:
  Si >40% de las primeras 10 páginas tienen <30 chars de texto → modo OCR.
  Las páginas mixtas (texto + imagen) se fusionan: texto PyMuPDF + OCR gap.

Hardware objetivo: Ryzen 5 5600X (6C/12T) + RTX 3060 Ti 8 GB GDDR6 448 GB/s
  • Conversiones texto: ThreadPoolExecutor hasta 12 workers (1 por thread HW)
  • OCR GPU: semáforo interno de 2 workers para no saturar 8 GB VRAM
=============================================================================
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("bookdork.pdf_engine")

# ─────────────────────────────────────────────────────────────────────────────
# Detección de dependencias opcionales (todas con fallback gracioso)
# ─────────────────────────────────────────────────────────────────────────────

# PyMuPDF (fitz) se importa de forma EAGER: es la dependencia base para todo
# PDF, su import es barato (~50 ms) y todo proceso del pool la necesita.
try:
    import fitz as _fitz          # PyMuPDF
    _PYMUPDF_OK = True
    logger.info("PyMuPDF %s disponible — ruta rápida PDF activa.", _fitz.version[0])
except ImportError:
    _fitz       = None
    _PYMUPDF_OK = False
    logger.info("PyMuPDF no disponible. Instala con: pip install pymupdf")

# ─────────────────────────────────────────────────────────────────────────────
# Imports PESADOS diferidos (torch / easyocr / markitdown)
# ─────────────────────────────────────────────────────────────────────────────
# Importar torch en el top-level cuesta 3–5 s y carga CUDA. En Windows el
# ProcessPoolExecutor usa 'spawn', así que CADA proceso del pool reimportaba el
# módulo entero y pagaba ese coste en el arranque — aunque solo fuera a procesar
# PDFs digitales (que jamás tocan la GPU). Diferimos estos imports a la primera
# necesidad real: los procesos que solo hacen texto/EPUB arrancan en milisegundos
# y el coste de torch se paga UNA vez, en el proceso principal, durante prewarm().
#
# `_CUDA_OK` / `_MARKITDOWN_OK` usan None como centinela de "aún no probado".

_torch          = None
_CUDA_OK: Optional[bool] = None      # None = sin probar; True/False = resultado
_md_engine      = None
_MARKITDOWN_OK: Optional[bool] = None


def _probe_cuda() -> bool:
    """Detecta CUDA de forma diferida y cachea el resultado en `_CUDA_OK`.

    Idempotente y barato tras la primera llamada. Importar torch aquí (y no en
    el top-level) evita que los procesos del pool sin OCR carguen CUDA.
    """
    global _torch, _CUDA_OK
    if _CUDA_OK is not None:
        return _CUDA_OK
    try:
        import torch as _t
        _torch  = _t
        _CUDA_OK = bool(_t.cuda.is_available())
        if _CUDA_OK:
            name = _t.cuda.get_device_name(0)
            mem  = _t.cuda.get_device_properties(0).total_memory // (1024 ** 3)
            logger.info("GPU detectada: %s (%d GB VRAM) — OCR acelerado por GPU activo.",
                        name, mem)
        else:
            logger.info("CUDA no disponible — OCR usará CPU si se activa.")
    except ImportError:
        _torch   = None
        _CUDA_OK = False
    return _CUDA_OK


def _ensure_markitdown():
    """Carga MarkItDown de forma diferida. Devuelve el engine o None.

    El motor se construye una sola vez y se cachea en `_md_engine`.
    """
    global _md_engine, _MARKITDOWN_OK
    if _MARKITDOWN_OK is not None:
        return _md_engine
    try:
        import warnings as _warnings
        with _warnings.catch_warnings():
            _warnings.filterwarnings(
                "ignore", message="Couldn't find ffmpeg", category=RuntimeWarning)
            from markitdown import MarkItDown as _MarkItDown
            _md_engine = _MarkItDown()
        _MARKITDOWN_OK = True
    except ImportError:
        _md_engine     = None
        _MARKITDOWN_OK = False
    return _md_engine

# ─────────────────────────────────────────────────────────────────────────────
# Constantes de configuración
# ─────────────────────────────────────────────────────────────────────────────

_SCANNED_THRESHOLD = 0.40   # >40% páginas sin texto → modo OCR
_MIN_TEXT_CHARS    = 30     # mínimo de chars para considerar página "con texto"
_OCR_SAMPLE_PAGES  = 10     # páginas de muestra para detectar tipo de PDF

# ── STEM routing ─────────────────────────────────────────────────────────────
# Densidad STEM mínima para activar el pipeline analítico de alta fidelidad.
# 0.05 = 5% de "signal" matemático ya justifica el costo extra (stem_engine
# es ~1.3× el costo de extracción plana pero recupera super/sub, fixes mojibake,
# normaliza Unicode y emite Markdown estructurado).
_STEM_DENSITY_THRESHOLD = 0.05

# Permite desactivar el motor STEM vía env var sin tocar código (fallback seguro)
_STEM_ENABLED = os.environ.get("BOOKDORK_STEM_ENGINE", "1").lower() not in (
    "0", "false", "no", "off",
)

# Importación lazy del motor STEM: si las deps no están, el sistema cae al
# pipeline tradicional sin levantar excepción al importar pdf_engine.
_stem_engine = None


def _get_stem_engine():
    global _stem_engine
    if _stem_engine is not None or not _STEM_ENABLED:
        return _stem_engine
    # Intentar primero import relativo (cuando se carga como parte del paquete
    # backend), luego absoluto (cuando se carga el módulo aislado en tests).
    try:
        from . import stem_engine as _se
        _stem_engine = _se
        logger.info("✓ stem_engine cargado (pipeline STEM disponible).")
        return _stem_engine
    except ImportError:
        pass
    try:
        import stem_engine as _se  # type: ignore[import]
        _stem_engine = _se
        logger.info("✓ stem_engine cargado (modo standalone).")
        return _stem_engine
    except ImportError as exc:
        logger.info("stem_engine no disponible (%s) — pipeline tradicional.", exc)
    return _stem_engine


# ─────────────────────────────────────────────────────────────────────────────
# Configuración runtime (defaults; el server las sobrescribe desde Settings)
# ─────────────────────────────────────────────────────────────────────────────

class _OCRConfig:
    """Mutable holder for runtime OCR settings.

    Permite que `main.py` inyecte valores desde Settings sin que pdf_engine
    importe el módulo config (evita import circular).
    """
    dpi:             int   = 200
    pages_parallel:  int   = 3
    gpu_concurrency: int   = 3
    gpu_timeout_s:   float = 8.0
    force_gpu:       bool  = False   # True = OCR nunca cae a CPU (máxima fidelidad)


_cfg = _OCRConfig()


def configure(*, dpi: int = 200, pages_parallel: int = 3,
              gpu_concurrency: int = 3, gpu_timeout_s: float = 8.0,
              force_gpu: bool = False) -> None:
    """Llamado desde lifespan tras leer Settings. Idempotente."""
    global _gpu_sem
    _cfg.dpi             = max(72, int(dpi))
    _cfg.pages_parallel  = max(1, int(pages_parallel))
    _cfg.gpu_concurrency = max(1, int(gpu_concurrency))
    _cfg.gpu_timeout_s   = max(0.0, float(gpu_timeout_s))
    _cfg.force_gpu       = bool(force_gpu)
    # Re-crear el semáforo con la nueva concurrencia.
    _gpu_sem = threading.Semaphore(_cfg.gpu_concurrency)
    logger.info(
        "OCR config: DPI=%d, pages_parallel=%d, gpu_concurrency=%d, "
        "gpu_timeout=%.1fs, force_gpu=%s",
        _cfg.dpi, _cfg.pages_parallel, _cfg.gpu_concurrency,
        _cfg.gpu_timeout_s, _cfg.force_gpu,
    )


# Semáforo GPU: limitado por VRAM (8 GB / ~1.2 GB por sesión). Default 3.
_gpu_sem        = threading.Semaphore(_cfg.gpu_concurrency)
_ocr_reader_gpu = None       # singleton GPU
_ocr_reader_cpu = None       # singleton CPU (fallback bajo presión)
_ocr_lock       = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# API pública
# ─────────────────────────────────────────────────────────────────────────────

def is_available() -> bool:
    """True si al menos un motor de conversión está disponible.

    Corto-circuita en PyMuPDF para no forzar el import de MarkItDown en el
    chequeo (caso normal: PyMuPDF presente).
    """
    return _PYMUPDF_OK or (_ensure_markitdown() is not None)


def convert_document(content: bytes, ext: str, filename: str) -> dict:
    """
    Convierte un documento a Markdown seleccionando la ruta óptima.

    Args:
        content:  Bytes del archivo.
        ext:      Extensión con punto, ej. '.pdf', '.epub'.
        filename: Nombre original del archivo (para mensajes de error y metadatos).

    Returns:
        dict con:
          success          bool
          markdown         str  (solo si success=True)
          char_count       int
          word_count       int
          original_filename str
          md_filename      str
          engine_used      str  ('pymupdf', 'easyocr-cuda', 'easyocr-cpu', 'markitdown')
          pdf_metadata     dict (solo para PDFs via PyMuPDF)
    """
    if ext.lower() == ".pdf":
        return _route_pdf(content, filename)
    return _convert_markitdown(content, ext, filename)


def get_engine_status() -> dict:
    """Devuelve el estado de cada motor para el endpoint /api/health.

    Fuerza las probes diferidas (torch / markitdown) para reportar el estado
    real. Solo se llama en el proceso principal y rara vez (health check), así
    que el coste de import diferido aquí es irrelevante.
    """
    return {
        "pymupdf":        _PYMUPDF_OK,
        "cuda":           _probe_cuda(),
        "markitdown":     _ensure_markitdown() is not None,
        "ocr_gpu_ready":  _ocr_reader_gpu is not None,
        "ocr_cpu_ready":  _ocr_reader_cpu is not None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Routing interno
# ─────────────────────────────────────────────────────────────────────────────

def _route_pdf(content: bytes, filename: str) -> dict:
    if _PYMUPDF_OK:
        return _convert_pdf_pymupdf(content, filename)
    return _convert_markitdown(content, ".pdf", filename)


def _try_stem_engine(content: bytes, filename: str, doc) -> Optional[dict]:
    """
    Intenta convertir con stem_engine si:
      1. El motor STEM está habilitado y disponible
      2. El documento tiene densidad STEM ≥ threshold

    Cualquier excepción se silencia → caller cae al pipeline tradicional.
    """
    se = _get_stem_engine()
    if se is None:
        return None
    try:
        density = se.score_stem_density(doc)
    except Exception as exc:
        logger.warning("stem_engine.score_stem_density falló (%s) — fallback", exc)
        return None
    if density < _STEM_DENSITY_THRESHOLD:
        logger.debug("STEM density %.3f < threshold %.3f — ruta normal",
                     density, _STEM_DENSITY_THRESHOLD)
        return None
    logger.info("STEM density %.3f ≥ %.3f — activando stem_engine para '%s'",
                density, _STEM_DENSITY_THRESHOLD, filename)
    try:
        result = se.convert_pdf_stem(content, filename)
        if result and result.get("success"):
            return result
        logger.warning("stem_engine devolvió sin éxito — fallback")
    except Exception as exc:
        logger.warning("stem_engine.convert_pdf_stem excepción (%s) — fallback", exc)
    return None


def _convert_pdf_pymupdf(content: bytes, filename: str) -> dict:
    """
    Extracción rápida con PyMuPDF.
    Detecta páginas escaneadas y las envía a OCR si es necesario.
    """
    doc      = _fitz.open(stream=content, filetype="pdf")
    metadata = _extract_pdf_meta(doc)
    n_pages  = len(doc)
    sample   = min(_OCR_SAMPLE_PAGES, n_pages)

    # Extrae el texto de las páginas de muestra UNA sola vez y lo cachea: sirve
    # tanto para la detección de escaneado como para el bucle de extracción
    # posterior. Antes estas páginas se extraían dos veces (en PDFs pequeños eso
    # casi duplicaba el coste). Mismo get_text/flags → salida idéntica.
    sample_texts: dict[int, str] = {}
    img_pages = 0
    for i in range(sample):
        t = doc[i].get_text("text").strip()
        sample_texts[i] = t
        if len(t) < _MIN_TEXT_CHARS:
            img_pages += 1
    scanned_ratio = img_pages / max(sample, 1)

    if scanned_ratio > _SCANNED_THRESHOLD:
        logger.info(
            "PDF escaneado detectado (%.0f%% páginas sin texto): %s",
            scanned_ratio * 100, filename,
        )
        ocr_result = _convert_pdf_ocr(doc, filename, metadata)
        if ocr_result:
            return ocr_result
        logger.warning(
            "OCR no disponible para PDF escaneado '%s'. "
            "Instala: pip install easyocr torch torchvision", filename,
        )

    # ── PDF digital: intentar primero el motor STEM si aplica ────────────────
    stem_result = _try_stem_engine(content, filename, doc)
    if stem_result:
        # stem_engine no rellena metadata extraída por nosotros; injectamos
        stem_result.setdefault("pdf_metadata", metadata)
        return stem_result

    # ── Ruta texto tradicional: extrae todas las páginas con PyMuPDF ─────────
    # Reutiliza el texto ya extraído para las páginas de muestra; solo extrae de
    # nuevo las páginas no muestreadas (índices ≥ sample).
    parts = []
    for i in range(n_pages):
        text = sample_texts[i] if i in sample_texts else doc[i].get_text("text").strip()
        if text:
            parts.append(text)

    markdown = "\n\n---\n\n".join(parts).strip()

    if not markdown:
        return {
            "success":           False,
            "original_filename": filename,
            "error": (
                "Sin texto extraíble. El PDF parece estar completamente escaneado. "
                "Activa OCR instalando: pip install torch torchvision --index-url "
                "https://download.pytorch.org/whl/cu128 && pip install easyocr"
            ),
        }

    return _build_ok(markdown, filename, metadata, "pymupdf")


def _render_page_grayscale(page, dpi: int):
    """Renderiza una página PDF a array numpy uint8 escala de grises."""
    import numpy as np
    mat = _fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=_fitz.csGRAY)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)


def _ocr_page_with_fallback(reader_gpu, page_idx: int, arr, filename: str):
    """
    Ejecuta OCR sobre un array. Intenta GPU con timeout; si no se obtiene
    el slot en _cfg.gpu_timeout_s segundos cae a CPU automáticamente.
    Devuelve (page_idx, texto, engine_used) o (page_idx, "", error_str).

    Si _cfg.force_gpu está activo y hay reader GPU, NUNCA cae a CPU: espera el
    slot GPU de forma bloqueante y, ante error de GPU, devuelve error en vez de
    degradar la calidad con el modelo CPU (que puede divergir en bordes).
    """
    # 0. Modo solo-GPU: espera el slot sin timeout, sin fallback a CPU.
    if _cfg.force_gpu and reader_gpu is not None:
        with _gpu_sem:
            try:
                txts = reader_gpu.readtext(arr, detail=0, paragraph=True)
                return page_idx, "\n".join(txts) if txts else "", "easyocr-cuda"
            except Exception as exc:
                logger.warning("OCR GPU error pág %d de '%s' (force_gpu, sin fallback): %s",
                               page_idx, filename, exc)
                return page_idx, "", f"error:{type(exc).__name__}"

    # 1. Intento GPU con timeout configurable
    if reader_gpu is not None and _cfg.gpu_timeout_s > 0:
        acquired = _gpu_sem.acquire(timeout=_cfg.gpu_timeout_s)
        if acquired:
            try:
                txts = reader_gpu.readtext(arr, detail=0, paragraph=True)
                return page_idx, "\n".join(txts) if txts else "", "easyocr-cuda"
            except Exception as exc:
                logger.warning("OCR GPU error pág %d de '%s': %s — fallback CPU",
                               page_idx, filename, exc)
            finally:
                _gpu_sem.release()
    elif reader_gpu is not None:
        # gpu_timeout_s == 0 → blocking sin timeout (legacy)
        with _gpu_sem:
            try:
                txts = reader_gpu.readtext(arr, detail=0, paragraph=True)
                return page_idx, "\n".join(txts) if txts else "", "easyocr-cuda"
            except Exception as exc:
                logger.warning("OCR GPU error pág %d: %s — fallback CPU", page_idx, exc)

    # 2. Fallback a CPU (carga reader lazy la primera vez)
    reader_cpu = _get_ocr_reader(prefer_gpu=False)
    if reader_cpu is None:
        return page_idx, "", "ocr-unavailable"
    try:
        txts = reader_cpu.readtext(arr, detail=0, paragraph=True)
        return page_idx, "\n".join(txts) if txts else "", "easyocr-cpu"
    except Exception as exc:
        logger.warning("OCR CPU error pág %d de '%s': %s", page_idx, filename, exc)
        return page_idx, "", f"error:{type(exc).__name__}"


def _convert_pdf_ocr(doc, filename: str, metadata: dict) -> Optional[dict]:
    """
    OCR con EasyOCR — GPU principal, CPU como apoyo.

    Pipeline optimizado:
      1. ThreadPoolExecutor con _cfg.pages_parallel workers.
      2. Cada worker: renderiza su página en CPU (CPU-bound) y luego compite
         por el semáforo GPU para OCR. Al haber N workers, mientras uno
         espera GPU, los otros renderizan → overlap CPU/GPU.
      3. Si el semáforo GPU no se libera en _cfg.gpu_timeout_s, ese worker
         cae a OCR CPU. Mantiene throughput aunque la GPU esté saturada.
      4. Resultados se reordenan por page_idx para preservar el orden del PDF.

    Returns None si no hay reader ni GPU ni CPU disponible.
    """
    try:
        import numpy as np  # noqa: F401 — verificación de disponibilidad
    except ImportError:
        logger.warning("numpy no disponible — OCR cancelado.")
        return None

    # Preferimos GPU si disponible; si no, el reader CPU se cargará lazy en _ocr_page_with_fallback.
    cuda_ok    = _probe_cuda()
    reader_gpu = _get_ocr_reader(prefer_gpu=True) if cuda_ok else None
    if reader_gpu is None and not cuda_ok:
        # CUDA ausente → todo OCR va a CPU. Pre-cargamos el reader CPU una vez.
        if _cfg.force_gpu:
            logger.warning(
                "OCR_FORCE_GPU activo pero CUDA no disponible para '%s' — "
                "no se hará OCR (sin degradar a CPU).", filename)
            return None
        if _get_ocr_reader(prefer_gpu=False) is None:
            return None

    n_pages   = len(doc)
    pages_par = max(1, min(_cfg.pages_parallel, n_pages))
    dpi       = _cfg.dpi

    # Pre-renderizar y dispatch en threads. Los resultados llegan en cualquier
    # orden — el dict por page_idx los reordena al final.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results: dict[int, tuple[str, str]] = {}

    def _task(idx: int) -> tuple[int, str, str]:
        arr = _render_page_grayscale(doc[idx], dpi)
        return _ocr_page_with_fallback(reader_gpu, idx, arr, filename)

    engines_used: set[str] = set()
    with ThreadPoolExecutor(max_workers=pages_par,
                            thread_name_prefix="ocr-page") as ex:
        futures = [ex.submit(_task, i) for i in range(n_pages)]
        for fut in as_completed(futures):
            idx, text, engine = fut.result()
            results[idx] = (text, engine)
            engines_used.add(engine)

    parts = [results[i][0] for i in range(n_pages) if results.get(i, ("", ""))[0]]
    markdown = "\n\n---\n\n".join(parts).strip()
    if not markdown:
        return None

    # Engine reportado: prioriza CUDA si alguna página la usó, sino CPU.
    if "easyocr-cuda" in engines_used:
        engine = "easyocr-cuda" + ("+cpu-fallback" if "easyocr-cpu" in engines_used else "")
    elif "easyocr-cpu" in engines_used:
        engine = "easyocr-cpu"
    else:
        engine = "easyocr"

    return _build_ok(markdown, filename, metadata, engine)


def _convert_markitdown(content: bytes, ext: str, filename: str) -> dict:
    """
    Ruta de compatibilidad: MarkItDown soporta EPUB, MOBI, AZW3, DjVu, TXT y PDF.
    Crea un archivo temporal (MarkItDown requiere ruta en disco).
    """
    md_engine = _ensure_markitdown()
    if md_engine is None:
        return {
            "success":           False,
            "original_filename": filename,
            "error":             "Motor de conversión no disponible. Ejecuta: pip install 'markitdown[pdf]'",
        }

    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        result   = md_engine.convert(tmp_path)
        markdown = (result.text_content or "").strip()

        if not markdown:
            return {
                "success":           False,
                "original_filename": filename,
                "error":             "Sin texto extraíble — archivo dañado o formato incompatible.",
            }

        return _build_ok(markdown, filename, {}, "markitdown")

    except Exception as exc:
        return {
            "success":           False,
            "original_filename": filename,
            "error":             f"{type(exc).__name__}: {exc}",
        }
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_ok(markdown: str, filename: str, meta: dict, engine: str) -> dict:
    return {
        "success":           True,
        "original_filename": filename,
        "md_filename":       Path(filename).stem + ".md",
        "markdown":          markdown,
        "char_count":        len(markdown),
        "word_count":        len(markdown.split()),
        "pdf_metadata":      meta,
        "engine_used":       engine,
    }


def _extract_pdf_meta(doc) -> dict:
    """Extrae campos estándar de metadatos del PDF (no siempre presentes)."""
    raw = doc.metadata or {}
    return {
        "title":    (raw.get("title")    or "").strip(),
        "author":   (raw.get("author")   or "").strip(),
        "subject":  (raw.get("subject")  or "").strip(),
        "keywords": (raw.get("keywords") or "").strip(),
        "pages":    len(doc),
    }


def _get_ocr_reader(prefer_gpu: bool = True):
    """
    Devuelve el lector EasyOCR. Dos singletons separados:
      * _ocr_reader_gpu — cargado en VRAM (~1.17 GB) si CUDA disponible.
      * _ocr_reader_cpu — cargado en RAM (~700 MB), fallback bajo carga.

    Args:
        prefer_gpu: si True intenta GPU primero. False fuerza CPU
                    (usado por el path de fallback cuando GPU saturado).
    """
    global _ocr_reader_gpu, _ocr_reader_cpu
    use_gpu = prefer_gpu and _probe_cuda()

    with _ocr_lock:
        current = _ocr_reader_gpu if use_gpu else _ocr_reader_cpu
        if current is not None:
            return current
        try:
            import easyocr
            mode = "GPU CUDA" if use_gpu else "CPU"
            logger.info("Cargando modelo EasyOCR (%s)…", mode)
            reader = easyocr.Reader(
                ["es", "en"],
                gpu=use_gpu,
                model_storage_directory=str(Path.home() / ".easyocr"),
                download_enabled=True,
                verbose=False,
            )
            logger.info("EasyOCR listo (%s).", mode)
            if use_gpu:
                _ocr_reader_gpu = reader
            else:
                _ocr_reader_cpu = reader
            return reader
        except ImportError:
            logger.info(
                "EasyOCR no instalado. Para activar OCR: "
                "pip install easyocr torch torchvision"
            )
        except Exception as exc:
            logger.warning("Error inicializando EasyOCR (%s): %s",
                           "GPU" if use_gpu else "CPU", exc)
    return None


def prewarm() -> bool:
    """
    Pre-carga el modelo EasyOCR (GPU si disponible, sino CPU) en background-friendly.
    Llamado desde lifespan al arrancar el server. Devuelve True si carga exitosa.

    Sin esto, la primera conversión escaneada paga ~1.5 s de cold start.
    """
    reader = _get_ocr_reader(prefer_gpu=True)
    return reader is not None
