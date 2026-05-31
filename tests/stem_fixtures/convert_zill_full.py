"""
Conversión completa del Zill PDF (724 págs) con stem_engine + font_recovery.

Genera:
  - zill_full.md         markdown enhanced
  - zill_recovery_report.json
       métricas comparativas: defectos antes/después, símbolos recuperados,
       breakdown por categoría (Greek, math operators, control bytes)
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND.resolve()))

for m in ("font_recovery", "stem_engine", "pdf_engine"):
    spec = importlib.util.spec_from_file_location(m, BACKEND / f"{m}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[m] = mod
    spec.loader.exec_module(mod)

import pdf_engine     # type: ignore
import stem_engine    # type: ignore

HERE = Path(__file__).parent
ZILL_PDF = Path(r"C:/Users/nicof/Downloads/ecuaciones-diferenciales-zill-vol-1.pdf")
PRE_MD = Path(r"C:/Users/nicof/Downloads/ecuaciones-diferenciales-zill-vol-1.md")
OUT_MD = HERE / "zill_full.md"
OUT_JSON = HERE / "zill_recovery_report.json"

GREEK_LOWER = "αβγδεζηθικλμνξοπρστυφχψω"
GREEK_UPPER = "ΓΔΘΛΞΠΣΥΦΨΩ"
MATH_OPS = "=−+×÷±·∓≠≤≥≈≡≅∼∝<>∗⋅⊕⊖⊗∂∇∫∮∑∏√∞→←↑↓↔⇒⇐⇔"


def analyze(md: str) -> dict:
    """Cuenta defectos y símbolos recuperados en el markdown."""
    control = sum(1 for c in md if 0 < ord(c) < 0x20 and c not in "\t\n\r")
    return {
        "chars":             len(md),
        "words":             len(md.split()),
        "lines":             md.count("\n"),
        "null_bytes":        md.count("\x00"),
        "control_bytes":     control,
        "ligatures_intact":  sum(md.count(c) for c in "ﬁﬀﬂﬃﬄ"),
        "soft_hyphens":      md.count("­"),
        "hyphen_newline":    md.count("-\n"),
        "headings":          sum(1 for ln in md.splitlines() if ln.lstrip().startswith("#")),
        "marker_caret_brace":   md.count("^{"),
        "marker_underscore_brace": md.count("_{"),
        "greek_lower":       sum(md.count(c) for c in GREEK_LOWER),
        "greek_upper":       sum(md.count(c) for c in GREEK_UPPER),
        "math_operators":    sum(md.count(c) for c in MATH_OPS),
    }


def main():
    if not ZILL_PDF.exists():
        print(f"FATAL: PDF no encontrado en {ZILL_PDF}")
        return 1

    print("═" * 72)
    print("  Zill PDF — conversión completa con stem_engine + font_recovery")
    print("═" * 72)
    content = ZILL_PDF.read_bytes()
    print(f"  Input PDF: {ZILL_PDF.name}  ({ZILL_PDF.stat().st_size:,} bytes)")

    t0 = time.perf_counter()
    result = pdf_engine.convert_document(content, ".pdf", ZILL_PDF.name)
    elapsed = time.perf_counter() - t0

    if not result.get("success"):
        print(f"  ✗ Conversión falló: {result.get('error')}")
        return 1

    md = result["markdown"]
    OUT_MD.write_text(md, encoding="utf-8")
    enhanced = analyze(md)

    print(f"  ✓ Engine: {result['engine_used']}")
    print(f"  ✓ Tiempo: {elapsed:.1f}s  ({elapsed/result.get('pdf_metadata',{}).get('pages',1):.2f}s/pág)")
    print(f"  ✓ Output MD: {OUT_MD}")
    print()

    # Comparar contra el MD original (pre-stem_engine)
    if PRE_MD.exists():
        pre_md = PRE_MD.read_text(encoding="utf-8")
        baseline = analyze(pre_md)
        print("═" * 72)
        print("  COMPARACIÓN — Baseline (pre-pipeline) vs Enhanced (con font_recovery)")
        print("═" * 72)
        labels = [
            ("Defectos", "control_bytes",    "less"),
            ("",          "null_bytes",        "less"),
            ("",          "ligatures_intact",  "less"),
            ("",          "hyphen_newline",    "less"),
            ("Markers",   "headings",          "more"),
            ("",          "marker_caret_brace","more"),
            ("",          "marker_underscore_brace","more"),
            ("Símbolos",  "greek_lower",       "more"),
            ("",          "greek_upper",       "more"),
            ("",          "math_operators",    "more"),
        ]
        print(f"{'Métrica':<25} {'Baseline':>12} {'Enhanced':>12} {'Δ':>14}")
        print("─" * 70)
        for cat, key, better in labels:
            b = baseline[key]; e = enhanced[key]
            delta = e - b
            arrow = "✓" if (delta > 0 and better == "more") or (delta < 0 and better == "less") else ("○" if delta == 0 else "⚠")
            print(f"{cat:<10}{key:<15} {b:>12,} {e:>12,} {arrow} {delta:+,}")
    else:
        baseline = None
        print("(MD pre-pipeline no disponible — no se compara)")

    # JSON report
    report = {
        "pdf_file": str(ZILL_PDF),
        "pages": result.get("pdf_metadata", {}).get("pages"),
        "engine_used": result["engine_used"],
        "elapsed_seconds": round(elapsed, 2),
        "enhanced_metrics": enhanced,
        "baseline_metrics": baseline,
    }
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"  JSON report: {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
