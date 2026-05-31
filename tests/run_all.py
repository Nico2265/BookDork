"""
=============================================================================
run_all.py — Runner unificado de QA para BookDork
=============================================================================
Ejecuta TODAS las suites de test del proyecto, cada una en su PROPIO
subproceso. El aislamiento por proceso es deliberado: varias suites mutan
os.environ["BOOKDORK_STEM_ENGINE"] y recargan módulos vía sys.modules
(pdf_engine/stem_engine/fidelity_metrics/stem_validator). Correrlas en el
mismo intérprete las haría dependientes del orden; en subprocesos, cada una
parte de un estado limpio.

Agrega exit codes: devuelve 0 solo si TODAS pasan. Pensado para CI y para un
chequeo local de un comando.

Ejecutar:  python -X utf8 tests/run_all.py
           python -X utf8 tests/run_all.py -q     # salida resumida
=============================================================================
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
TESTS = Path(__file__).parent

# Orden: unitarios rápidos → recuperación → gate de conversión (lento, abre PDFs).
SUITES: list[tuple[str, Path]] = [
    ("stem_fidelity (glifo, determinista)", TESTS / "stem_fixtures" / "test_stem_fidelity.py"),
    ("glyph_name_recovery (33 casos)",      TESTS / "test_glyph_name_recovery.py"),
    ("font_harvester (30 casos)",           TESTS / "test_font_harvester.py"),
    ("font_table_recovery (22 casos)",      TESTS / "test_font_table_recovery.py"),
    ("font_recovery",                       TESTS / "stem_fixtures" / "test_font_recovery.py"),
    ("no_regression (prosa bit-exacta)",    TESTS / "stem_fixtures" / "test_no_regression.py"),
    ("conversion_improvement (GATE)",       TESTS / "stem_fixtures" / "test_conversion_improvement.py"),
]


def run_suite(label: str, path: Path, quiet: bool) -> tuple[bool, float, str]:
    if not path.exists():
        return False, 0.0, f"archivo no encontrado: {path}"
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(path)],
        capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
    )
    elapsed = time.perf_counter() - t0
    ok = proc.returncode == 0
    output = proc.stdout + (("\n" + proc.stderr) if proc.stderr.strip() else "")
    if not ok and quiet:
        # En modo quiet mostramos la salida solo de las que fallan (para diagnóstico).
        output = output
    return ok, elapsed, output


def main(argv: list[str]) -> int:
    quiet = "-q" in argv or "--quiet" in argv
    print("█" * 72)
    print("  BookDork — QA suite completa")
    print("█" * 72)

    results: list[tuple[str, bool, float]] = []
    for label, path in SUITES:
        print(f"\n▶ {label}")
        ok, elapsed, output = run_suite(label, path, quiet)
        if not quiet or not ok:
            # Indentamos la salida de la suite para legibilidad.
            for ln in output.rstrip().splitlines():
                print(f"    {ln}")
        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"  {status}  ({elapsed:.1f}s)")
        results.append((label, ok, elapsed))

    print("\n" + "█" * 72)
    print("  RESUMEN")
    print("█" * 72)
    total_t = sum(r[2] for r in results)
    n_pass = sum(1 for r in results if r[1])
    for label, ok, elapsed in results:
        mark = "✓" if ok else "✗"
        print(f"  {mark}  {label:<44s} {elapsed:6.1f}s")
    print("─" * 72)
    print(f"  {n_pass}/{len(results)} suites verdes   ·   {total_t:.1f}s total")
    print("█" * 72)
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
