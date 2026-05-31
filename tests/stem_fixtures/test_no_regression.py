"""
Test de no-regresión.

Verifica DOS invariantes:

  A) Para un PDF de prosa (sin matemática):
     - STEM density < threshold
     - engine_used = "pymupdf"  (no se activa stem_engine)
     - El output es BIT-EXACTO al output con BOOKDORK_STEM_ENGINE=0
       (es decir, la presencia del nuevo módulo no altera el pipeline
       tradicional)

  B) Para un PDF STEM:
     - engine_used = "stem-analytical" cuando el motor está habilitado
     - engine_used = "pymupdf"          cuando BOOKDORK_STEM_ENGINE=0
     - Ambos resultados son válidos (exitosos), pero el STEM es mejor;
       verificamos solo que el flag honor funciona.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND.resolve()))


def _fresh_import_pdf_engine():
    """
    Forza una reimportación limpia de pdf_engine para que el flag
    BOOKDORK_STEM_ENGINE se lea en el momento del import.
    """
    for m in ("pdf_engine", "stem_engine"):
        sys.modules.pop(m, None)
    for m in ("stem_engine", "pdf_engine"):
        spec = importlib.util.spec_from_file_location(m, BACKEND / f"{m}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[m] = mod
        spec.loader.exec_module(mod)
    return sys.modules["pdf_engine"], sys.modules["stem_engine"]


def convert_with_flag(content: bytes, ext: str, name: str, stem_enabled: bool) -> dict:
    """Convierte refrescando módulos para honrar el flag de entorno."""
    os.environ["BOOKDORK_STEM_ENGINE"] = "1" if stem_enabled else "0"
    pdf_engine, _ = _fresh_import_pdf_engine()
    return pdf_engine.convert_document(content, ext, name)


def test_prose_no_regression():
    content = (Path(__file__).parent / "prose_sample.pdf").read_bytes()

    # Importación inicial para acceder a score_stem_density
    os.environ["BOOKDORK_STEM_ENGINE"] = "1"
    _, stem_engine = _fresh_import_pdf_engine()

    import fitz
    doc = fitz.open(stream=content, filetype="pdf")
    density = stem_engine.score_stem_density(doc)
    doc.close()
    print(f"  Prose STEM density: {density:.4f}  (threshold 0.05)")
    assert density < 0.05

    # Convertir con flag ON y OFF — para prosa, deben ser idénticos porque
    # density < threshold y stem_engine no se invoca.
    res_on  = convert_with_flag(content, ".pdf", "prose.pdf", stem_enabled=True)
    res_off = convert_with_flag(content, ".pdf", "prose.pdf", stem_enabled=False)

    assert res_on["success"] and res_off["success"]
    assert res_on["engine_used"] == "pymupdf", (
        f"❌ REGRESIÓN: prosa rutea a {res_on['engine_used']}"
    )
    assert res_off["engine_used"] == "pymupdf"
    assert res_on["markdown"] == res_off["markdown"], (
        "❌ REGRESIÓN: salida con STEM_ENGINE=1 difiere de STEM_ENGINE=0 "
        "para PDF de prosa"
    )
    print("  ✓ engine_used = pymupdf (no STEM activado)")
    print("  ✓ Output bit-exacto con/sin flag — pipeline tradicional intacto")


def test_stem_routing():
    content = (Path(__file__).parent / "math_textbook_sample.pdf").read_bytes()

    res_on  = convert_with_flag(content, ".pdf", "math.pdf", stem_enabled=True)
    res_off = convert_with_flag(content, ".pdf", "math.pdf", stem_enabled=False)

    assert res_on["success"] and res_off["success"]
    assert res_on["engine_used"] == "stem-analytical", (
        f"❌ STEM PDF no activó stem_engine: engine={res_on['engine_used']}"
    )
    assert res_off["engine_used"] == "pymupdf", (
        f"❌ Flag OFF no respetado: engine={res_off['engine_used']}"
    )
    print("  ✓ STEM PDF con flag ON  → stem-analytical")
    print("  ✓ STEM PDF con flag OFF → pymupdf (fallback respetado)")


def main():
    print("─" * 60)
    print("Test A: PDF de prosa (sin matemática)")
    print("─" * 60)
    test_prose_no_regression()
    print()
    print("─" * 60)
    print("Test B: routing STEM con flag de entorno")
    print("─" * 60)
    test_stem_routing()
    print()
    print("═" * 60)
    print("✓ NO-REGRESIÓN COMPLETA — todos los invariantes preservados")
    print("═" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
