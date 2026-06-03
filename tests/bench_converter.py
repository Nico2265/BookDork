"""
=============================================================================
bench_converter.py — Benchmark end-to-end de latencia de conversión (QA)
=============================================================================
Reproduce la ruta de PRODUCCIÓN (no un mock):
  • ProcessPoolExecutor con afinidad, igual que el lifespan de main.py.
  • Pre-warm del pool antes de medir.
  • Conversión vía backend.main._sync_convert (la misma función que usa el
    endpoint real, ejecutada en los procesos del pool).

Mide:
  1. Tiempo de pre-warm (spawn + import de los workers).
  2. Latencia por archivo para PDFs de 1 / ~25 / ~120 páginas.
  3. Latencia en frío (sin pre-warm) vs caliente del primer archivo.

Verifica el objetivo: cada conversión individual < 5 s.

Uso:  python tests/bench_converter.py
=============================================================================
"""
from __future__ import annotations

import concurrent.futures
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TARGET_SECONDS = 5.0
LOREM = (
    "El rapido zorro marron salta sobre el perro perezoso mientras el sol "
    "ilumina el valle. La conversion de documentos a Markdown debe preservar "
    "la estructura, los parrafos y la informacion tecnica del libro original. "
)


def _make_pdf(n_pages: int, lines_per_page: int = 45) -> bytes:
    """Genera un PDF de texto realista de n_pages páginas con PyMuPDF.

    Usa insert_text línea a línea (no insert_textbox, que descarta el texto si
    desborda la caja) para garantizar texto realmente extraíble — si no, el
    motor lo trataría como escaneado y mediríamos OCR en vez de la ruta rápida.
    """
    import fitz
    line = LOREM.strip()
    doc = fitz.open()
    for p in range(n_pages):
        page = doc.new_page()
        page.insert_text((50, 40), f"Capitulo {p + 1}", fontsize=14)
        y = 70
        for ln in range(lines_per_page):
            page.insert_text((50, y), f"{ln + 1}. {line}", fontsize=9)
            y += 15
            if y > 800:
                break
    data = doc.tobytes()
    doc.close()

    # QA guard: el fixture DEBE tener texto extraíble, o el benchmark no mide
    # lo que cree medir (ruta PyMuPDF digital, no OCR).
    check = fitz.open(stream=data, filetype="pdf")
    extractable = sum(len(check[i].get_text("text").strip()) for i in range(len(check)))
    check.close()
    assert extractable > 200 * n_pages, (
        f"fixture inválido: solo {extractable} chars extraíbles en {n_pages} págs"
    )
    return data


def _fmt(secs: float) -> str:
    return f"{secs * 1000:6.0f} ms" if secs < 1 else f"{secs:6.2f} s "


def _build_pool(workers: int) -> concurrent.futures.ProcessPoolExecutor:
    return concurrent.futures.ProcessPoolExecutor(max_workers=workers)


def main() -> int:
    # Windows: la consola por defecto (cp1252) no codifica ✓/→; forzamos UTF-8.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    from backend.main import _sync_convert, _pool_warmup

    workers = 4
    print("=" * 70)
    print(f"BENCHMARK CONVERTIDOR — objetivo < {TARGET_SECONDS:.0f} s por archivo")
    print(f"ProcessPool: {workers} procesos | plataforma: {sys.platform}")
    print("=" * 70)

    # ── Fixtures de distintos tamaños ────────────────────────────────────────
    print("\nGenerando PDFs de prueba…")
    fixtures = {
        "1 pág   (~0.0 MB)": _make_pdf(1),
        "25 págs (~0.1 MB)": _make_pdf(25),
        "120 págs(~0.5 MB)": _make_pdf(120),
    }
    for name, data in fixtures.items():
        print(f"  {name}: {len(data) / 1024:.0f} KB")

    # ── 1) Latencia EN FRÍO (pool recién creado, sin pre-warm) ───────────────
    print("\n[1] Primer archivo EN FRÍO (pool sin pre-warm)…")
    cold_pool = _build_pool(workers)
    sample = fixtures["1 pág   (~0.0 MB)"]
    t0 = time.perf_counter()
    cold_pool.submit(_sync_convert, sample, ".pdf", "cold.pdf").result()
    cold_first = time.perf_counter() - t0
    cold_pool.shutdown(wait=True)
    print(f"    primer convert en frío: {_fmt(cold_first)}  (incluye spawn+import)")

    # ── 2) Pre-warm del pool (como en producción) ────────────────────────────
    print("\n[2] Pre-warm del pool (spawn + import anticipado)…")
    pool = _build_pool(workers)
    t0 = time.perf_counter()
    for f in [pool.submit(_pool_warmup) for _ in range(workers)]:
        f.result(timeout=60)
    warm = time.perf_counter() - t0
    print(f"    pre-warm de {workers} procesos: {_fmt(warm)}")

    # ── 3) Latencia por tamaño con pool CALIENTE (N repeticiones) ────────────
    print("\n[3] Latencia con pool CALIENTE (5 reps por tamaño):")
    print(f"    {'archivo':<20} {'p50':>10} {'p95':>10} {'max':>10}  estado")
    print(f"    {'-' * 20} {'-' * 10} {'-' * 10} {'-' * 10}  ------")
    all_ok = True
    reps = 5
    for name, data in fixtures.items():
        times = []
        for i in range(reps):
            t0 = time.perf_counter()
            res = pool.submit(_sync_convert, data, ".pdf", f"{name}.pdf").result()
            times.append(time.perf_counter() - t0)
            assert res["success"], f"conversión falló: {res.get('error')}"
        p50 = statistics.median(times)
        p95 = max(times) if reps < 20 else statistics.quantiles(times, n=20)[18]
        mx = max(times)
        ok = mx < TARGET_SECONDS
        all_ok = all_ok and ok
        flag = "OK ✓" if ok else "LENTO ✗"
        print(f"    {name:<20} {_fmt(p50)} {_fmt(p95)} {_fmt(mx)}  {flag}")

    # ── 4) Lote concurrente de 5 archivos (caso real: subida múltiple) ───────
    print("\n[4] Lote de 5 archivos en paralelo (subida múltiple real):")
    batch = list(fixtures.values()) + [fixtures["25 págs (~0.1 MB)"], fixtures["1 pág   (~0.0 MB)"]]
    t0 = time.perf_counter()
    futs = [pool.submit(_sync_convert, d, ".pdf", f"batch{i}.pdf") for i, d in enumerate(batch)]
    for f in futs:
        assert f.result()["success"]
    batch_t = time.perf_counter() - t0
    batch_ok = batch_t < TARGET_SECONDS
    print(f"    5 archivos en paralelo: {_fmt(batch_t)}  {'OK ✓' if batch_ok else 'LENTO ✗'}")

    pool.shutdown(wait=True)

    # ── Resumen ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"Cold start (sin pre-warm): {_fmt(cold_first)}")
    print(f"Pre-warm:                  {_fmt(warm)}  → elimina el cold start del 1er usuario")
    print(f"Veredicto individual:      {'TODAS < 5s ✓' if all_ok else 'ALGUNA ≥ 5s ✗'}")
    print(f"Veredicto lote (5 files):  {'< 5s ✓' if batch_ok else '≥ 5s ✗'}")
    print("=" * 70)

    return 0 if (all_ok and batch_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
