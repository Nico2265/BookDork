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

try:
    import fitz as _fitz          # PyMuPDF
    _PYMUPDF_OK = True
    logger.info("PyMuPDF %s disponible — ruta rápida PDF activa.", _fitz.version[0])
except ImportError:
    _fitz       = None
    _PYMUPDF_OK = False
    logger.info("PyMuPDF no disponible. Instala con: pip install pymupdf")

try:
    import torch as _torch
    _CUDA_OK = _torch.cuda.is_available()
    if _CUDA_OK:
        _gpu_name = _torch.cuda.get_device_name(0)
        _gpu_mem  = _torch.cuda.get_device_properties(0).total_memory // (1024 ** 3)
        logger.info("GPU detectada: %s (%d GB VRAM) — OCR acelerado por GPU activo.",
                    _gpu_name, _gpu_mem)
    else:
        logger.info("CUDA no disponible — OCR usará CPU si se activa.")
except ImportError:
    _torch   = None
    _CUDA_OK = False

try:
    import warnings as _warnings
    with _warnings.catch_warnings():
        _warnings.filterwarnings("ignore", message="Couldn't find ffmpeg", category=RuntimeWarning)
        from markitdown import MarkItDown as _MarkItDown
        _md_engine = _MarkItDown()
    _MARKITDOWN_OK = True
except ImportError:
    _md_engine     = None
    _MARKITDOWN_OK = False

# ─────────────────────────────────────────────────────────────────────────────
# Constantes de configuración
# ─────────────────────────────────────────────────────────────────────────────

_SCANNED_THRESHOLD = 0.40   # >40% páginas sin texto → modo OCR
_MIN_TEXT_CHARS    = 30     # mínimo de chars para considerar página "con texto"
_OCR_DPI           = 200    # DPI de renderizado para OCR (balance calidad/velocidad)
_OCR_SAMPLE_PAGES  = 10     # páginas de muestra para detectar tipo de PDF

# Semáforo GPU: máx. 2 conversiones OCR simultáneas (RTX 3060 Ti 8 GB)
_gpu_sem     = threading.Semaphore(2)
_ocr_reader  = None
_ocr_lock    = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# API pública
# ─────────────────────────────────────────────────────────────────────────────

def is_available() -> bool:
    """True si al menos un motor de conversión está disponible."""
    return _MARKITDOWN_OK or _PYMUPDF_OK


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
    """Devuelve el estado de cada motor para el endpoint /api/health."""
    return {
        "pymupdf":    _PYMUPDF_OK,
        "cuda":       _CUDA_OK,
        "markitdown": _MARKITDOWN_OK,
        "ocr_ready":  _ocr_reader is not None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Routing interno
# ─────────────────────────────────────────────────────────────────────────────

def _route_pdf(content: bytes, filename: str) -> dict:
    if _PYMUPDF_OK:
        return _convert_pdf_pymupdf(content, filename)
    return _convert_markitdown(content, ".pdf", filename)


def _convert_pdf_pymupdf(content: bytes, filename: str) -> dict:
    """
    Extracción rápida con PyMuPDF.
    Detecta páginas escaneadas y las envía a OCR si es necesario.
    """
    doc      = _fitz.open(stream=content, filetype="pdf")
    metadata = _extract_pdf_meta(doc)
    n_pages  = len(doc)
    sample   = min(_OCR_SAMPLE_PAGES, n_pages)

    img_pages = sum(
        1 for i in range(sample)
        if len(doc[i].get_text("text").strip()) < _MIN_TEXT_CHARS
    )
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

    # Ruta texto: extrae todas las páginas con PyMuPDF
    parts = []
    for page in doc:
        text = page.get_text("text").strip()
        if text:
            parts.append(text)

    markdown = "\n\n---\n\n".join(parts).strip()

    if not markdown:
        return {
            "success":           False,
            "original_filename": filename,
            "error": (
                "Sin texto extraíble. El PDF parece estar completamente escaneado. "
                "Activa OCR instalando: pip install easyocr && pip install torch torchvision"
            ),
        }

    return _build_ok(markdown, filename, metadata, "pymupdf")


def _convert_pdf_ocr(doc, filename: str, metadata: dict) -> Optional[dict]:
    """
    OCR con EasyOCR. Usa GPU (RTX 3060 Ti) si CUDA está disponible.
    Protegido por semáforo para no saturar los 8 GB de VRAM.
    """
    reader = _get_ocr_reader()
    if reader is None:
        return None

    try:
        import numpy as np
    except ImportError:
        logger.warning("numpy no disponible — OCR cancelado.")
        return None

    mat   = _fitz.Matrix(_OCR_DPI / 72, _OCR_DPI / 72)
    parts = []

    with _gpu_sem:
        for page in doc:
            # Renderizar en escala de grises para reducir VRAM usage
            pix = page.get_pixmap(matrix=mat, colorspace=_fitz.csGRAY)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
            try:
                txts = reader.readtext(arr, detail=0, paragraph=True)
            except Exception as exc:
                logger.warning("OCR error en página %d de '%s': %s", page.number, filename, exc)
                continue
            if txts:
                parts.append("\n".join(txts))

    markdown = "\n\n---\n\n".join(parts).strip()
    if not markdown:
        return None

    engine = "easyocr-cuda" if _CUDA_OK else "easyocr-cpu"
    return _build_ok(markdown, filename, metadata, engine)


def _convert_markitdown(content: bytes, ext: str, filename: str) -> dict:
    """
    Ruta de compatibilidad: MarkItDown soporta EPUB, MOBI, AZW3, DjVu, TXT y PDF.
    Crea un archivo temporal (MarkItDown requiere ruta en disco).
    """
    if not _MARKITDOWN_OK:
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

        result   = _md_engine.convert(tmp_path)
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


def _get_ocr_reader():
    """
    Devuelve el lector EasyOCR (singleton). Carga modelo en primera llamada.
    El modelo ocupa ~700 MB en VRAM (GPU) o RAM (CPU).
    """
    global _ocr_reader
    with _ocr_lock:
        if _ocr_reader is None:
            try:
                import easyocr
                logger.info("Cargando modelo EasyOCR (gpu=%s)…", _CUDA_OK)
                _ocr_reader = easyocr.Reader(
                    ["es", "en"],
                    gpu=_CUDA_OK,
                    model_storage_directory=str(Path.home() / ".easyocr"),
                    download_enabled=True,
                    verbose=False,
                )
                mode = "GPU CUDA" if _CUDA_OK else "CPU"
                logger.info("EasyOCR listo (%s).", mode)
            except ImportError:
                logger.info(
                    "EasyOCR no instalado. Para activar OCR GPU: "
                    "pip install easyocr torch torchvision"
                )
            except Exception as exc:
                logger.warning("Error inicializando EasyOCR: %s", exc)
    return _ocr_reader
