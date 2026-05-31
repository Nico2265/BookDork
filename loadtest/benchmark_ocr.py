"""
benchmark_ocr.py — Compara EasyOCR en CPU vs GPU sobre el mismo PDF.

Mide:
  * Tiempo de inicialización del Reader (cold start, carga modelo).
  * Tiempo de inferencia por página.
  * Tiempo total wall-clock.
  * Caracteres extraídos (sanity check: no debe ser 0).

Salida:
  Tabla legible + JSON estructurado.

Uso:
    python loadtest/benchmark_ocr.py [--pdf <ruta>] [--pages N]
"""
from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from pathlib import Path

import fitz       # PyMuPDF
import numpy as np


def load_pdf_as_grayscale_arrays(pdf_path: Path, max_pages: int, dpi: int = 200):
    """Render cada página del PDF como ndarray uint8 en escala de grises."""
    doc = fitz.open(str(pdf_path))
    arrays = []
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        arrays.append(arr)
    doc.close()
    return arrays


def bench_mode(arrays: list, gpu: bool) -> dict:
    """Corre EasyOCR sobre `arrays` con el modo dado y mide tiempos."""
    import easyocr

    label = "GPU (CUDA)" if gpu else "CPU"
    print(f"\n=== Modo {label} ===")
    gc.collect()

    t0 = time.perf_counter()
    reader = easyocr.Reader(
        ["es", "en"],
        gpu=gpu,
        verbose=False,
        download_enabled=True,
    )
    t_init = time.perf_counter() - t0
    print(f"[init] modelo cargado en {t_init:.2f} s")

    per_page_s = []
    total_chars = 0
    t_inf_start = time.perf_counter()

    for i, arr in enumerate(arrays):
        t_pg_start = time.perf_counter()
        result = reader.readtext(arr, detail=0, paragraph=True)
        dt = time.perf_counter() - t_pg_start
        per_page_s.append(dt)
        chars = sum(len(line) for line in result)
        total_chars += chars
        print(f"  page {i + 1:>2}: {dt * 1000:>6.0f} ms  ({chars} chars)")

    t_inf_total = time.perf_counter() - t_inf_start

    # Liberar VRAM/RAM antes de la siguiente prueba
    del reader
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass

    return {
        "mode":             label,
        "init_s":           round(t_init, 2),
        "pages":            len(arrays),
        "total_inference_s": round(t_inf_total, 2),
        "mean_per_page_ms": round(sum(per_page_s) / len(per_page_s) * 1000, 1) if per_page_s else 0,
        "p95_per_page_ms":  round(sorted(per_page_s)[int(len(per_page_s) * 0.95)] * 1000, 1) if per_page_s else 0,
        "total_chars":      total_chars,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default="loadtest/sample_scanned.pdf")
    ap.add_argument("--pages", type=int, default=10)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--output", default="loadtest/results_ocr_bench.json")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    pdf = root / args.pdf
    if not pdf.exists():
        print(f"[error] no existe {pdf}. Generá primero con gen_scanned_pdf.py", file=sys.stderr)
        return 2

    print(f"[input] {pdf}  ({pdf.stat().st_size / 1024:.0f} KB)")
    print(f"[render] {args.pages} páginas @ {args.dpi} DPI a escala de grises...")
    arrays = load_pdf_as_grayscale_arrays(pdf, args.pages, args.dpi)
    print(f"[ready] {len(arrays)} arrays en RAM")

    # Detectar disponibilidad de GPU
    try:
        import torch
        cuda_ok = torch.cuda.is_available()
        gpu_name = torch.cuda.get_device_name(0) if cuda_ok else "n/a"
    except ImportError:
        cuda_ok = False
        gpu_name = "torch no instalado"

    print(f"\n[env] torch CUDA disponible: {cuda_ok} — {gpu_name}")

    results = {}
    # CPU primero (más lento — al hacerlo primero el OS cache está frío,
    # luego GPU corre con cache de filesystem caliente. Para comparación
    # equitativa lo correcto es repetirlo, pero estos números aún son útiles).
    results["cpu"] = bench_mode(arrays, gpu=False)

    if cuda_ok:
        results["gpu"] = bench_mode(arrays, gpu=True)
        gpu_ms = results["gpu"]["mean_per_page_ms"]
        cpu_ms = results["cpu"]["mean_per_page_ms"]
        if gpu_ms > 0:
            results["speedup"] = round(cpu_ms / gpu_ms, 2)
    else:
        results["gpu"] = {"mode": "GPU (CUDA)", "status": "SKIPPED (CUDA not available)"}
        results["speedup"] = None

    # Tabla resumen
    print("\n" + "=" * 60)
    print(f"{'Mode':<12}{'Init s':>10}{'Total s':>12}{'Avg/page ms':>14}")
    print("-" * 60)
    for key in ("cpu", "gpu"):
        r = results[key]
        if r.get("status"):
            print(f"{r['mode']:<12}{r['status']:>40}")
        else:
            print(f"{r['mode']:<12}{r['init_s']:>10}{r['total_inference_s']:>12}"
                  f"{r['mean_per_page_ms']:>14}")
    if results.get("speedup"):
        print(f"\n>>> Speedup GPU vs CPU: {results['speedup']}×  <<<")

    out_path = root / args.output
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[output] {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
