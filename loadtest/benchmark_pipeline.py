"""
benchmark_pipeline.py — Mide el pipeline OCR completo de pdf_engine.

A diferencia de benchmark_ocr.py (que medía sólo EasyOCR.readtext), este
script invoca pdf_engine.convert_document — el mismo path que usa el server
en producción. Captura ganancias del pipeline (paralelismo, fallback, prewarm)
encima del speedup raw del GPU.

Uso:
    python loadtest/benchmark_pipeline.py [--pdf <ruta>]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_pdf_engine():
    spec = importlib.util.spec_from_file_location(
        "pdf_engine_bench", ROOT / "backend" / "pdf_engine.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def bench(pdf_path: Path, label: str, configure_kwargs: dict | None,
          prewarm: bool) -> dict:
    """Una iteración: opcionalmente configura, prewarmea, convierte y mide."""
    print(f"\n=== {label} ===")
    pdf_engine = _load_pdf_engine()

    if configure_kwargs:
        pdf_engine.configure(**configure_kwargs)
        print(f"[configure] {configure_kwargs}")

    if prewarm:
        t_warm = time.perf_counter()
        ok = pdf_engine.prewarm()
        print(f"[prewarm] success={ok} in {time.perf_counter() - t_warm:.2f} s")

    content = pdf_path.read_bytes()
    t0 = time.perf_counter()
    result = pdf_engine.convert_document(content, ".pdf", pdf_path.name)
    dt = time.perf_counter() - t0

    success = result.get("success", False)
    engine  = result.get("engine_used", "?")
    chars   = result.get("char_count", 0)

    print(f"[convert] success={success} engine={engine} chars={chars} time={dt:.2f} s")

    return {
        "label":   label,
        "success": success,
        "engine":  engine,
        "chars":   chars,
        "time_s":  round(dt, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default="loadtest/sample_scanned.pdf")
    ap.add_argument("--output", default="loadtest/results_pipeline.json")
    args = ap.parse_args()

    pdf_path = ROOT / args.pdf
    if not pdf_path.exists():
        print(f"[error] no existe {pdf_path}", file=sys.stderr)
        return 2
    print(f"[input] {pdf_path}  ({pdf_path.stat().st_size / 1024:.0f} KB)")

    # ── Escenario A: secuencial (baseline equivalente al pre-fix) ────────────
    # pages_parallel=1, gpu_concurrency=1 → comportamiento legacy 1 página a la vez.
    a = bench(
        pdf_path, "A: secuencial (legacy)",
        {"dpi": 200, "pages_parallel": 1, "gpu_concurrency": 1, "gpu_timeout_s": 8.0},
        prewarm=False,  # legacy también pagaba cold start
    )

    # ── Escenario B: pipeline optimizado (defaults nuevos) ───────────────────
    b = bench(
        pdf_path, "B: pipeline optimizado (3 paralelo + pre-warm)",
        {"dpi": 200, "pages_parallel": 3, "gpu_concurrency": 3, "gpu_timeout_s": 8.0},
        prewarm=True,
    )

    # ── Escenario C: pipeline + DPI bajado a 150 ─────────────────────────────
    c = bench(
        pdf_path, "C: pipeline + DPI 150 (más rápido, calidad cercana)",
        {"dpi": 150, "pages_parallel": 3, "gpu_concurrency": 3, "gpu_timeout_s": 8.0},
        prewarm=True,
    )

    results = {"a": a, "b": b, "c": c}

    # ── Comparativa ──────────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print(f"{'Scenario':<45}{'time s':>10}{'chars':>10}{'speedup':>10}")
    print("-" * 72)
    base = a["time_s"]
    for r in (a, b, c):
        speedup = round(base / r["time_s"], 2) if r["time_s"] > 0 else 0
        print(f"{r['label']:<45}{r['time_s']:>10.2f}{r['chars']:>10}"
              f"{speedup:>9.2f}x")

    out_path = ROOT / args.output
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    print(f"\n[output] {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
