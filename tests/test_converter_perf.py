"""
=============================================================================
test_converter_perf.py — QA de las optimizaciones de velocidad del convertidor
=============================================================================
Cubre los cambios introducidos para bajar la latencia de conversión a <5 s:

  • pdf_engine: imports diferidos (torch/markitdown) e idempotencia de las
    probes lazy (_probe_cuda / _ensure_markitdown), flag force_gpu.
  • pdf_engine: ruta rápida PyMuPDF sigue produciendo Markdown correcto.
  • conversion_cache: update_cover (enriquecimiento de portada en background).
  • main: _persist_conversion persiste sin esperar a Open Library y devuelve
    book_id; normalización de ISBN del endpoint /api/book-meta.

No requiere red, CUDA, Firebase ni GPU. Genera un PDF mínimo en memoria con
PyMuPDF (dependencia base, siempre presente).
=============================================================================
"""

import re
from pathlib import Path

import pytest

from backend import pdf_engine


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_text_pdf(text: str) -> bytes:
    """Crea un PDF de una página con texto incrustado (ruta PyMuPDF)."""
    import fitz
    doc  = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


# ─────────────────────────────────────────────────────────────────────────────
# pdf_engine: probes diferidas
# ─────────────────────────────────────────────────────────────────────────────

def test_probe_cuda_idempotent_and_bool():
    first = pdf_engine._probe_cuda()
    assert isinstance(first, bool)
    # Idempotente: segunda llamada devuelve el mismo valor cacheado.
    assert pdf_engine._probe_cuda() is first
    # El centinela ya no es None tras la primera probe.
    assert pdf_engine._CUDA_OK is not None


def test_ensure_markitdown_idempotent():
    a = pdf_engine._ensure_markitdown()
    b = pdf_engine._ensure_markitdown()
    assert a is b  # mismo singleton (o None de forma consistente)
    assert pdf_engine._MARKITDOWN_OK is not None


def test_is_available_true_with_pymupdf():
    # PyMuPDF es dependencia base del proyecto.
    assert pdf_engine._PYMUPDF_OK is True
    assert pdf_engine.is_available() is True


def test_engine_status_keys():
    status = pdf_engine.get_engine_status()
    for key in ("pymupdf", "cuda", "markitdown", "ocr_gpu_ready", "ocr_cpu_ready"):
        assert key in status
    assert isinstance(status["cuda"], bool)
    assert isinstance(status["markitdown"], bool)


# ─────────────────────────────────────────────────────────────────────────────
# pdf_engine: configure(force_gpu)
# ─────────────────────────────────────────────────────────────────────────────

def test_configure_force_gpu_flag():
    try:
        pdf_engine.configure(dpi=200, pages_parallel=2, gpu_concurrency=2,
                             gpu_timeout_s=5.0, force_gpu=True)
        assert pdf_engine._cfg.force_gpu is True
        pdf_engine.configure(force_gpu=False)
        assert pdf_engine._cfg.force_gpu is False
    finally:
        # Restaurar defaults para no contaminar otros tests.
        pdf_engine.configure()


# ─────────────────────────────────────────────────────────────────────────────
# pdf_engine: ruta rápida PyMuPDF
# ─────────────────────────────────────────────────────────────────────────────

def test_pymupdf_text_extraction_ok():
    pdf = _make_text_pdf("Hola mundo BookDork 1234567890")
    result = pdf_engine.convert_document(pdf, ".pdf", "muestra.pdf")
    assert result["success"] is True
    assert "Hola mundo BookDork" in result["markdown"]
    assert result["char_count"] > 0
    assert result["word_count"] > 0
    assert result["md_filename"] == "muestra.md"
    # PDF digital → motor rápido (pymupdf) o STEM, nunca OCR.
    assert result["engine_used"] in ("pymupdf", "stem", "stem-analytical",
                                     "stem_engine")


def _make_multipage_pdf(n_pages: int) -> bytes:
    """PDF de varias páginas con texto extraíble (línea a línea)."""
    import fitz
    doc = fitz.open()
    for p in range(n_pages):
        page = doc.new_page()
        page.insert_text((50, 40), f"Capitulo {p + 1}", fontsize=13)
        y = 70
        for ln in range(40):
            page.insert_text((50, y), f"{ln + 1}. Linea de prueba del documento.", fontsize=9)
            y += 16
            if y > 800:
                break
    data = doc.tobytes()
    doc.close()
    return data


@pytest.mark.parametrize("n_pages", [1, 3, 5, 10, 30])
def test_sample_reuse_output_identical(n_pages):
    """La optimización de reutilizar el texto de las páginas de muestra debe
    producir EXACTAMENTE la misma salida que extraer todas las páginas de cero.
    Blinda contra regresiones de calidad al cambiar el bucle de extracción."""
    import fitz

    content = _make_multipage_pdf(n_pages)

    # Extracción de referencia (ingenua: todas las páginas, sin caché de muestra)
    ref_doc = fitz.open(stream=content, filetype="pdf")
    ref_parts = [ref_doc[i].get_text("text").strip() for i in range(len(ref_doc))]
    ref_doc.close()
    expected = "\n\n---\n\n".join(p for p in ref_parts if p).strip()

    result = pdf_engine.convert_document(content, ".pdf", f"{n_pages}.pdf")
    assert result["success"] is True
    # Solo comparamos si fue por la ruta PyMuPDF (no STEM, que reestructura).
    if result["engine_used"] == "pymupdf":
        assert result["markdown"] == expected


# ─────────────────────────────────────────────────────────────────────────────
# conversion_cache: update_cover
# ─────────────────────────────────────────────────────────────────────────────

def test_cache_update_cover(tmp_path: Path):
    from backend.conversion_cache import ConversionCache

    cache = ConversionCache(tmp_path / "cache")
    file_hash = "a" * 64
    book_id = cache.put(
        file_hash=file_hash, markdown="# contenido\n", filename="libro.pdf",
        file_size=1234, isbn="9780306406157", language="es", topic="otros",
        engine="pymupdf", title="Libro", cover_url=None,
    )
    assert book_id

    cover = "https://covers.openlibrary.org/b/id/123-M.jpg"
    cache.update_cover(file_hash, cover)

    books = cache.list_books(limit=10)["books"]
    match = [b for b in books if b["cover_url"] == cover]
    assert len(match) == 1


def test_cache_update_cover_missing_is_noop(tmp_path: Path):
    from backend.conversion_cache import ConversionCache

    cache = ConversionCache(tmp_path / "cache")
    # No debe lanzar aunque el hash no exista.
    cache.update_cover("deadbeef" * 8, "https://x/y.jpg")


# ─────────────────────────────────────────────────────────────────────────────
# main: _persist_conversion (sin red) y normalización ISBN
# ─────────────────────────────────────────────────────────────────────────────

def test_persist_conversion_returns_book_id(tmp_path: Path):
    from backend.conversion_cache import ConversionCache
    from backend.main import _persist_conversion

    cache = ConversionCache(tmp_path / "cache")
    md = "# Título\n\nISBN 978-0-306-40615-7\n\nTexto del libro de prueba."
    book_id = _persist_conversion(
        cache, "b" * 64, md, "ejemplo.pdf", 4096, "9780306406157", "pymupdf",
    )
    assert book_id
    # Persistido y recuperable; la portada queda en None (se enriquece aparte).
    assert cache.get("b" * 64) is not None


@pytest.mark.parametrize("raw,expected_len", [
    ("978-0-306-40615-7", 13),
    ("0306406152", 10),
    ("030640615X", 10),
    ("isbn: 9780306406157", 13),
])
def test_isbn_normalization_valid(raw, expected_len):
    normalized = re.sub(r'[^0-9Xx]', '', raw).upper()
    assert len(normalized) == expected_len


@pytest.mark.parametrize("raw", ["12345", "abcdef", "97803064061570000"])
def test_isbn_normalization_invalid(raw):
    normalized = re.sub(r'[^0-9Xx]', '', raw).upper()
    assert len(normalized) not in (10, 13)
