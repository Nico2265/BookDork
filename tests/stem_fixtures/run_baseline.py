"""
Ejecuta el pipeline ACTUAL (pdf_engine.py sin modificaciones) sobre el PDF de
prueba y guarda el resultado como baseline_output.md.

Esto establece la línea base con la que comparar después de implementar
stem_engine.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))   # para que pdf_engine pueda hacer "import stem_engine"

# Importar el módulo backend con el nombre que tiene en disco
import importlib.util
for mod_name in ("stem_engine", "pdf_engine"):  # stem_engine primero (dependencia)
    spec = importlib.util.spec_from_file_location(
        mod_name,
        BACKEND / f"{mod_name}.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)

import pdf_engine  # type: ignore

HERE = Path(__file__).parent
PDF = HERE / "math_textbook_sample.pdf"
OUT = HERE / "baseline_output.md"


def main():
    content = PDF.read_bytes()
    result = pdf_engine.convert_document(content, ".pdf", PDF.name)

    if not result.get("success"):
        print(f"FALLO: {result.get('error')}")
        return 1

    md = result["markdown"]
    OUT.write_text(md, encoding="utf-8")

    print(f"Engine usado:    {result.get('engine_used')}")
    print(f"Caracteres:      {result.get('char_count')}")
    print(f"Palabras:        {result.get('word_count')}")
    print(f"Output:          {OUT}")
    print()
    print("=" * 70)
    print("PRIMER 1500 CHARS DEL OUTPUT:")
    print("=" * 70)
    print(md[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
