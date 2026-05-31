"""
Benchmark completo: baseline vs enhanced sobre el PDF STEM.
Usa fidelity_metrics (módulo oficial) y score_fidelity (ground truth).
Genera bench_results.json con todos los datos para el reporte final.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND.resolve()))
HERE = Path(__file__).parent

STEM_PDF = HERE / "math_textbook_sample.pdf"
PROSE_PDF = HERE / "prose_sample.pdf"
GT_PATH = HERE / "ground_truth.json"


def fresh_import(stem_enabled: bool):
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


def run_one(content: bytes, label: str, stem_enabled: bool):
    pdf_engine, fidelity_metrics, stem_validator = fresh_import(stem_enabled)
    t0 = time.perf_counter()
    result = pdf_engine.convert_document(content, ".pdf", "sample.pdf")
    elapsed = time.perf_counter() - t0
    md = result.get("markdown", "")
    validation = stem_validator.validate_markdown(md)
    report = fidelity_metrics.compute(
        content, md,
        equation_validity=validation.validity_rate,
    )
    return {
        "label":      label,
        "engine":     result.get("engine_used"),
        "success":    result.get("success", False),
        "char_count": result.get("char_count", 0),
        "word_count": result.get("word_count", 0),
        "elapsed_s":  round(elapsed, 4),
        "markdown":   md,
        "fidelity":   report.to_dict(),
        "validation": {
            "total_equations":      validation.total_equations,
            "valid_equations":      validation.valid_equations,
            "validity_rate":        validation.validity_rate,
            "unbalanced_braces":    validation.unbalanced_braces,
            "orphan_markers":       validation.orphan_markers,
            "latex_parse_errors":   validation.latex_parse_errors,
        },
    }


def gt_score(md_path: Path) -> dict:
    """Llama al scorer de ground truth (score_fidelity.py)."""
    sys.path.insert(0, str(HERE))
    import score_fidelity  # type: ignore
    return score_fidelity.score(md_path, GT_PATH)


def main():
    content_stem = STEM_PDF.read_bytes()
    content_prose = PROSE_PDF.read_bytes()

    print("═" * 72)
    print("  BENCHMARK STEM PDF — Baseline (sin STEM engine) vs Enhanced")
    print("═" * 72)

    baseline = run_one(content_stem, "BASELINE (pymupdf)", stem_enabled=False)
    enhanced = run_one(content_stem, "ENHANCED (stem-analytical)", stem_enabled=True)

    # Score contra ground truth para ambos
    (HERE / "baseline_output.md").write_text(baseline["markdown"], encoding="utf-8")
    (HERE / "enhanced_output.md").write_text(enhanced["markdown"], encoding="utf-8")
    baseline_gt = gt_score(HERE / "baseline_output.md")
    enhanced_gt = gt_score(HERE / "enhanced_output.md")

    for r, gt_res in [(baseline, baseline_gt), (enhanced, enhanced_gt)]:
        print(f"\n──── {r['label']} ────")
        print(f"  engine          = {r['engine']}")
        print(f"  elapsed         = {r['elapsed_s']*1000:.1f} ms")
        print(f"  chars / words   = {r['char_count']} / {r['word_count']}")
        print(f"  fidelity.overall (no GT)    = {r['fidelity']['overall']*100:.2f}%")
        print(f"  fidelity.glyph_coverage     = {r['fidelity']['glyph_coverage']*100:.2f}%")
        print(f"  fidelity.mojibake_score     = {r['fidelity']['mojibake_score']*100:.2f}%")
        print(f"  fidelity.symbol_preservation= {r['fidelity']['symbol_preservation']*100:.2f}%")
        print(f"  fidelity.super_sub_detection= {r['fidelity']['super_sub_detection']*100:.2f}%")
        print(f"  ground_truth.overall        = {gt_res['overall']*100:.2f}%")
        print(f"  validation.equations        = {r['validation']['valid_equations']}/{r['validation']['total_equations']}  (validity={r['validation']['validity_rate']*100:.1f}%)")

    delta = (enhanced_gt["overall"] - baseline_gt["overall"]) * 100
    print()
    print("═" * 72)
    print(f"  Δ fidelity (ground truth): +{delta:.2f} percentage points")
    print(f"  Baseline → Enhanced : {baseline_gt['overall']*100:.2f}% → {enhanced_gt['overall']*100:.2f}%")
    print("═" * 72)

    # Test no-regresión sobre prosa
    print()
    print("═" * 72)
    print("  NO-REGRESIÓN — PDF de prosa pura")
    print("═" * 72)
    prose_on  = run_one(content_prose, "Prose flag=ON", stem_enabled=True)
    prose_off = run_one(content_prose, "Prose flag=OFF", stem_enabled=False)
    print(f"  Prose (flag ON ) engine = {prose_on['engine']}  fidelity = {prose_on['fidelity']['overall']*100:.2f}%")
    print(f"  Prose (flag OFF) engine = {prose_off['engine']}  fidelity = {prose_off['fidelity']['overall']*100:.2f}%")
    identical = prose_on["markdown"] == prose_off["markdown"]
    print(f"  Output identical: {identical}  ← debe ser True para no-regresión")

    # Persistir JSON
    out = {
        "stem_pdf": {
            "baseline":   {k: v for k, v in baseline.items() if k != "markdown"},
            "enhanced":   {k: v for k, v in enhanced.items() if k != "markdown"},
            "gt_baseline": baseline_gt,
            "gt_enhanced": enhanced_gt,
            "delta_overall_pp": round(delta, 2),
        },
        "prose_pdf": {
            "flag_on":   {k: v for k, v in prose_on.items() if k != "markdown"},
            "flag_off":  {k: v for k, v in prose_off.items() if k != "markdown"},
            "bit_exact": identical,
        },
    }
    (HERE / "bench_results.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print()
    print(f"  Bench JSON: {HERE / 'bench_results.json'}")


if __name__ == "__main__":
    main()
