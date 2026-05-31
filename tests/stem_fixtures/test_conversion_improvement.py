"""
=============================================================================
test_conversion_improvement.py — Gate automatizado: ¿mejoró la conversión?
=============================================================================
Convierte el benchmark baseline-vs-enhanced en un test pass/fail. Mientras
run_full_benchmark.py REPORTA números, este archivo los CONVIERTE EN ASSERTS
para que una regresión rompa la suite (CI) en lugar de pasar inadvertida.

Mide sobre el PDF STEM real (math_textbook_sample.pdf) dos pipelines:
  • BASELINE  : BOOKDORK_STEM_ENGINE=0  → pymupdf plano
  • ENHANCED  : BOOKDORK_STEM_ENGINE=1  → stem-analytical (los cambios)

Invariantes que se exigen para declarar "la conversión mejoró":
  1. Routing       — enhanced usa stem-analytical; baseline usa pymupdf.
  2. Ground truth  — enhanced ≥ MIN_ENHANCED_GT y al menos +MIN_DELTA_PP sobre
                     baseline (mejora significativa, no ruido).
  3. Super/sub     — enhanced detecta notación que el baseline pierde.
  4. Ecuaciones    — todas las ecuaciones detectadas son válidas (validity=1.0)
                     y hay al menos una (no es vacío trivial).
  5. Sin mojibake  — enhanced no introduce U+FFFD ni ligaduras sin expandir.
  6. No-regresión  — para prosa, el output es BIT-EXACTO con flag ON vs OFF.

Ejecutar:  python -X utf8 tests/stem_fixtures/test_conversion_improvement.py
(o vía pytest)
=============================================================================
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
BACKEND = ROOT / "backend"
HERE = Path(__file__).parent
sys.path.insert(0, str(BACKEND.resolve()))
sys.path.insert(0, str(HERE.resolve()))

STEM_PDF = HERE / "math_textbook_sample.pdf"
PROSE_PDF = HERE / "prose_sample.pdf"
GT_PATH = HERE / "ground_truth.json"

# Umbrales del gate. El benchmark actual da 42% → 100% (+58pp); fijamos pisos
# conservadores para tolerar variación de entorno sin dejar pasar regresiones.
MIN_ENHANCED_GT = 0.90      # enhanced debe alcanzar ≥90% de fidelidad GT
MIN_DELTA_PP    = 0.20      # mejora mínima de 20 puntos sobre baseline
MAX_BASELINE_GT = 0.75      # baseline (pymupdf plano) NO debería ya estar alto


def _fresh_import(stem_enabled: bool):
    os.environ["BOOKDORK_STEM_ENGINE"] = "1" if stem_enabled else "0"
    for m in ("pdf_engine", "stem_engine", "fidelity_metrics", "stem_validator"):
        sys.modules.pop(m, None)
    for m in ("stem_engine", "stem_validator", "fidelity_metrics", "pdf_engine"):
        spec = importlib.util.spec_from_file_location(m, BACKEND / f"{m}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[m] = mod
        spec.loader.exec_module(mod)
    return (
        sys.modules["pdf_engine"],
        sys.modules["fidelity_metrics"],
        sys.modules["stem_validator"],
    )


def _convert(content: bytes, stem_enabled: bool) -> dict:
    pdf_engine, fidelity_metrics, stem_validator = _fresh_import(stem_enabled)
    result = pdf_engine.convert_document(content, ".pdf", "sample.pdf")
    md = result.get("markdown", "")
    validation = stem_validator.validate_markdown(md)
    report = fidelity_metrics.compute(
        content, md, equation_validity=validation.validity_rate,
    )
    return {"result": result, "md": md, "validation": validation, "fidelity": report}


def _gt_overall(md: str, tag: str) -> float:
    import score_fidelity  # type: ignore
    path = HERE / f"_gate_{tag}.md"
    path.write_text(md, encoding="utf-8")
    try:
        return score_fidelity.score(path, GT_PATH)["overall"]
    finally:
        path.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_stem_conversion_improves_over_baseline():
    content = STEM_PDF.read_bytes()
    baseline = _convert(content, stem_enabled=False)
    enhanced = _convert(content, stem_enabled=True)

    # 1. Routing correcto
    assert baseline["result"]["engine_used"] == "pymupdf", baseline["result"]["engine_used"]
    assert enhanced["result"]["engine_used"] == "stem-analytical", enhanced["result"]["engine_used"]
    assert baseline["result"]["success"] and enhanced["result"]["success"]

    # 2. Ground truth: mejora significativa y piso alcanzado
    gt_base = _gt_overall(baseline["md"], "baseline")
    gt_enh = _gt_overall(enhanced["md"], "enhanced")
    delta = gt_enh - gt_base
    print(f"  GT baseline = {gt_base*100:.2f}%  enhanced = {gt_enh*100:.2f}%  Δ = +{delta*100:.2f}pp")
    assert gt_base <= MAX_BASELINE_GT, (
        f"baseline ya está en {gt_base*100:.1f}% > {MAX_BASELINE_GT*100:.0f}% — "
        f"el benchmark perdió poder discriminante"
    )
    assert gt_enh >= MIN_ENHANCED_GT, (
        f"enhanced {gt_enh*100:.1f}% < piso {MIN_ENHANCED_GT*100:.0f}% — ¿regresión?"
    )
    assert delta >= MIN_DELTA_PP, (
        f"mejora {delta*100:.1f}pp < mínimo {MIN_DELTA_PP*100:.0f}pp"
    )

    # 3. Super/sub: enhanced detecta notación que baseline pierde
    ss_base = baseline["fidelity"].super_sub_detection
    ss_enh = enhanced["fidelity"].super_sub_detection
    print(f"  super_sub_detection  baseline = {ss_base*100:.1f}%  enhanced = {ss_enh*100:.1f}%")
    assert ss_enh > ss_base, "super/sub no mejoró sobre baseline"

    # 4. Ecuaciones: todas válidas y al menos una
    v = enhanced["validation"]
    print(f"  ecuaciones válidas = {v.valid_equations}/{v.total_equations}")
    assert v.total_equations >= 1, "no se detectó ninguna ecuación en enhanced"
    assert v.valid_equations == v.total_equations, (
        f"ecuaciones inválidas: {v.total_equations - v.valid_equations} "
        f"(unbalanced={v.unbalanced_braces}, orphans={v.orphan_markers}, "
        f"latex_errors={v.latex_parse_errors})"
    )

    # 5. Sin mojibake en enhanced (prioridad #1: cero caracteres que engañen)
    assert enhanced["fidelity"].mojibake_score >= 0.99, (
        f"mojibake_score {enhanced['fidelity'].mojibake_score:.3f} — "
        f"caracteres rotos en la salida"
    )
    assert enhanced["fidelity"].symbol_preservation >= 0.95, (
        f"símbolos matemáticos perdidos: {enhanced['fidelity'].symbol_preservation:.3f}"
    )
    print("  ✓ conversión STEM mejora significativamente sobre baseline")


def test_prose_no_regression_bit_exact():
    content = PROSE_PDF.read_bytes()
    on = _convert(content, stem_enabled=True)
    off = _convert(content, stem_enabled=False)
    # Prosa rutea a pymupdf en ambos casos → output idéntico bit-a-bit.
    assert on["result"]["engine_used"] == "pymupdf", on["result"]["engine_used"]
    assert off["result"]["engine_used"] == "pymupdf"
    assert on["md"] == off["md"], (
        "❌ REGRESIÓN: el flag STEM altera la salida de prosa (debe ser bit-exacta)"
    )
    print("  ✓ prosa bit-exacta con flag ON vs OFF — pipeline tradicional intacto")


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [
        ("Mejora STEM sobre baseline",   test_stem_conversion_improves_over_baseline),
        ("No-regresión prosa bit-exact", test_prose_no_regression_bit_exact),
    ]
    print("═" * 64)
    print("GATE — ¿mejoró la conversión con los cambios STEM?")
    print("═" * 64)
    _saved_env = os.environ.get("BOOKDORK_STEM_ENGINE")
    failed = 0
    try:
        for name, fn in tests:
            try:
                print(f"\n──── {name} ────")
                fn()
            except AssertionError as exc:
                failed += 1
                print(f"  ✗ FALLO [{name}]: {exc}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  ✗ ERROR [{name}]: {type(exc).__name__}: {exc}")
    finally:
        # Higiene: no dejar el flag mutado para procesos/tests posteriores.
        if _saved_env is None:
            os.environ.pop("BOOKDORK_STEM_ENGINE", None)
        else:
            os.environ["BOOKDORK_STEM_ENGINE"] = _saved_env
    print("\n" + "═" * 64)
    if failed:
        print(f"✗ {failed}/{len(tests)} gates fallaron")
        return 1
    print(f"✓ {len(tests)}/{len(tests)} gates pasaron — la conversión mejoró y no regresó")
    return 0


if __name__ == "__main__":
    sys.exit(main())
