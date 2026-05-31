"""Ejecuta stem_engine sobre el PDF de prueba y guarda enhanced_output.md."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
spec = importlib.util.spec_from_file_location(
    "stem_engine",
    ROOT / "backend" / "stem_engine.py",
)
stem_engine = importlib.util.module_from_spec(spec)
sys.modules["stem_engine"] = stem_engine
spec.loader.exec_module(stem_engine)

HERE = Path(__file__).parent
PDF = HERE / "math_textbook_sample.pdf"
OUT = HERE / "enhanced_output.md"


def main():
    content = PDF.read_bytes()

    # Score STEM density primero
    import fitz
    doc = fitz.open(stream=content, filetype="pdf")
    density = stem_engine.score_stem_density(doc)
    doc.close()
    print(f"STEM density score: {density:.3f}")

    result = stem_engine.convert_pdf_stem(content, PDF.name)
    if not result.get("success"):
        print(f"FALLO: {result.get('error')}")
        return 1

    md = result["markdown"]
    OUT.write_text(md, encoding="utf-8")
    print(f"Engine:    {result['engine_used']}")
    print(f"Chars:     {result['char_count']}")
    print(f"Words:     {result['word_count']}")
    print(f"Output:    {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
