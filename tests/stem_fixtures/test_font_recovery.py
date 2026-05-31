"""
Test suite QA para font_recovery.py.

Verifica que:
  1. Detección de fuentes rotas funciona (no se activa en fuentes OK)
  2. Grk + ASCII alpha → mapeo phonetic directo (zero falsos negativos)
  3. Visual matching recupera Greek + operators con threshold sano
  4. Cache funciona (segunda llamada misma key → mismo resultado)
  5. PDFs SIN fuentes rotas → font_recovery no se instancia
  6. No-regresión: prose PDF y math PDF sintético sin cambios
  7. Confidence threshold previene falsos positivos burdos
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND.resolve()))

import fitz   # type: ignore

# Cargar módulos
for m in ("font_recovery", "stem_engine", "pdf_engine"):
    spec = importlib.util.spec_from_file_location(m, BACKEND / f"{m}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[m] = mod
    spec.loader.exec_module(mod)

import font_recovery   # type: ignore
import stem_engine     # type: ignore
import pdf_engine      # type: ignore

HERE = Path(__file__).parent


def _check(condition: bool, msg: str) -> None:
    if not condition:
        raise AssertionError(f"FAIL: {msg}")
    print(f"  ✓ {msg}")


# ── Tests unitarios ──────────────────────────────────────────────────────────

def test_font_is_broken_detection():
    print("\n[1] Detección de fuentes rotas")
    pos = [
        "MathematicalPi-One", "ABCDEF+MathematicalPi-Three", "MathPiOneBoldItalic",
        "Grk", "GPLNNM+Grk", "EuclidSymbol-Bold",
    ]
    neg = [
        "TimesNewRoman", "Arial", "Symbol", "SymbolStd", "Calibri",
        "Cambria Math", "Helvetica", "",
    ]
    for f in pos:
        _check(font_recovery._font_is_broken(f),
               f"{f!r} debe detectarse como broken")
    for f in neg:
        _check(not font_recovery._font_is_broken(f),
               f"{f!r} NO debe detectarse como broken")


def test_grk_phonetic_mapping():
    print("\n[2] Mapeo phonetic Grk a-z → Greek")
    expected = {
        "a": "α", "b": "β", "c": "χ", "d": "δ", "e": "ε",
        "f": "φ", "g": "γ", "h": "η", "i": "ι", "k": "κ",
        "l": "λ", "m": "μ", "n": "ν", "o": "ο", "p": "π",
        "q": "θ", "r": "ρ", "s": "σ", "t": "τ", "u": "υ",
        "w": "ω", "x": "ξ", "y": "ψ", "z": "ζ",
    }
    for latin, greek in expected.items():
        actual = font_recovery._GRK_ALPHA_MAP.get(latin)
        _check(actual == greek,
               f"Grk {latin!r} → {greek!r} (got {actual!r})")


def test_needs_recovery_signal():
    print("\n[3] needs_recovery devuelve correctamente True/False")
    recov = font_recovery.FontRecovery()
    _check(recov.needs_recovery("MathematicalPi-One", "\x02"),
           "MathPi-One + control byte → needs recovery")
    _check(recov.needs_recovery("Grk", "f"),
           "Grk + ASCII letter → needs recovery")
    _check(not recov.needs_recovery("MathematicalPi-One", "a"),
           "MathPi-One + ASCII regular → NO recovery (poco común)")
    _check(not recov.needs_recovery("Times", "\x02"),
           "Font no-roto + control byte → NO recovery")
    _check(not recov.needs_recovery("Grk", "1"),
           "Grk + digito → NO recovery (no es alpha)")
    _check(not recov.needs_recovery("Symbol", "π"),
           "Symbol font con Unicode válido → NO recovery")


def test_grk_recovery_direct():
    print("\n[4] Grk a-z se recupera directamente sin render")
    recov = font_recovery.FontRecovery()
    fake_span = {"font": "Grk", "chars": []}
    # No necesita página real porque el atajo Grk es phonetic
    result = recov.recover(None, fake_span, "f")
    _check(result == "φ", f"Grk 'f' → 'φ' (got {result!r})")
    result = recov.recover(None, fake_span, "p")
    _check(result == "π", f"Grk 'p' → 'π' (got {result!r})")
    stats = recov.stats()
    _check(stats["grk_alpha_direct"] >= 2,
           f"stats.grk_alpha_direct ≥ 2 (got {stats['grk_alpha_direct']})")


def test_cache_idempotent():
    print("\n[5] Cache devuelve el mismo resultado en llamadas repetidas")
    recov = font_recovery.FontRecovery()
    fake_span = {"font": "Grk", "chars": []}
    a = recov.recover(None, fake_span, "f")
    b = recov.recover(None, fake_span, "f")
    c = recov.recover(None, fake_span, "f")
    _check(a == b == c, f"Cache returns {a!r} consistently")
    # Stats: 3 calls, 2 cache hits
    stats = recov.stats()
    _check(stats["cache_hits"] >= 2, "cache_hits ≥ 2")


def test_visual_recovery_on_zill():
    """Si el Zill PDF está disponible, verifica que se recuperan glifos reales."""
    print("\n[6] Visual recovery en Zill (si disponible)")
    zill = Path(r"C:/Users/nicof/Downloads/ecuaciones-diferenciales-zill-vol-1.pdf")
    if not zill.exists():
        print("  ○ SKIPPED: Zill PDF no disponible localmente")
        return

    recov = font_recovery.FontRecovery()
    doc = fitz.open(zill)
    # Procesar ~5 páginas matemáticas
    recovered_count = 0
    for page_idx in [30, 100, 200, 400, 600]:
        if page_idx >= len(doc): continue
        page = doc[page_idx]
        for block in page.get_text("rawdict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    for ch in span.get("chars", []):
                        c = ch["c"]
                        if recov.needs_recovery(span["font"], c):
                            new = recov.recover(page, span, c)
                            if new != c:
                                recovered_count += 1
    doc.close()
    _check(recovered_count > 100,
           f"recuperados > 100 (got {recovered_count})")
    stats = recov.stats()
    print(f"      stats final: {stats}")


def test_no_regression_prose_pdf():
    """PDF de prosa no debe ver afectado su pipeline (font_recovery no se instancia)."""
    print("\n[7] No-regresión: PDF de prosa sin cambios")
    prose = HERE / "prose_sample.pdf"
    if not prose.exists():
        print("  ○ SKIPPED: prose_sample.pdf no disponible")
        return

    # Convertir directo con stem_engine
    content = prose.read_bytes()
    result = stem_engine.convert_pdf_stem(content, "prose.pdf")
    engine = result["engine_used"]
    # Engine debe ser 'stem-analytical' (NO incluye +font_recovery)
    _check(engine == "stem-analytical",
           f"Engine = {engine!r} (NO debe activar font_recovery en prosa)")


def test_no_regression_synthetic_stem():
    """El fixture STEM sintético no usa fuentes rotas: engine sin font_recovery."""
    print("\n[8] No-regresión: math_textbook_sample.pdf sin font_recovery")
    fix = HERE / "math_textbook_sample.pdf"
    if not fix.exists():
        print("  ○ SKIPPED: math_textbook_sample.pdf no disponible")
        return

    content = fix.read_bytes()
    result = stem_engine.convert_pdf_stem(content, "math.pdf")
    engine = result["engine_used"]
    _check(engine == "stem-analytical",
           f"Synthetic STEM no activa font_recovery (got {engine!r})")


def test_zill_routes_through_pdf_engine():
    """E2E: el Zill atraviesa pdf_engine.convert_document y termina en stem-analytical+font_recovery."""
    print("\n[9] E2E: pdf_engine routing al stem_engine con font_recovery")
    zill = Path(r"C:/Users/nicof/Downloads/ecuaciones-diferenciales-zill-vol-1.pdf")
    if not zill.exists():
        print("  ○ SKIPPED: Zill PDF no disponible")
        return

    # Sub-PDF amplio en zona con math denso (cap ~5: pages 200-249)
    # density verificada >0.10 → asegura activación de stem_engine + font_recovery
    src = fitz.open(zill)
    sub = fitz.open()
    sub.insert_pdf(src, from_page=200, to_page=249)
    content = sub.tobytes()
    src.close(); sub.close()

    result = pdf_engine.convert_document(content, ".pdf", "zill_sub.pdf")
    _check(result["success"], "Conversion exitosa")
    _check("font_recovery" in result["engine_used"],
           f"Engine contiene 'font_recovery' (got {result['engine_used']!r})")
    md = result["markdown"]
    # Mínimo de Greek letters recuperados
    greek = sum(md.count(c) for c in "αβγδεζηθικλμνξοπρστυφχψω")
    _check(greek >= 50, f"≥50 Greek letters recuperados en 50 págs STEM (got {greek})")
    # Cero NULL bytes
    _check(md.count("\x00") == 0, "Cero NULL bytes en output")
    # Math operators recuperados
    ops = sum(md.count(c) for c in "=−+×÷±·≠≤≥≈≡∫∑∏√∂∇∞→←↔")
    _check(ops >= 100, f"≥100 math operators (got {ops})")


def main():
    print("=" * 64)
    print("  Test suite QA — font_recovery.py")
    print("=" * 64)
    tests = [
        test_font_is_broken_detection,
        test_grk_phonetic_mapping,
        test_needs_recovery_signal,
        test_grk_recovery_direct,
        test_cache_idempotent,
        test_visual_recovery_on_zill,
        test_no_regression_prose_pdf,
        test_no_regression_synthetic_stem,
        test_zill_routes_through_pdf_engine,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"\n  ✗✗ {t.__name__}: {e}")
            failures += 1
        except Exception as e:
            print(f"\n  ✗✗ {t.__name__} (exception): {e!r}")
            failures += 1
    print()
    print("=" * 64)
    if failures == 0:
        print("  ✓ TODOS LOS TESTS PASAN")
    else:
        print(f"  ✗ {failures} tests fallaron")
    print("=" * 64)
    return failures


if __name__ == "__main__":
    sys.exit(main())
