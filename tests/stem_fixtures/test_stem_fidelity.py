"""
=============================================================================
test_stem_fidelity.py — QA de fidelidad matemática del stem_engine
=============================================================================
Tests deterministas a NIVEL GLIFO: construimos listas de `_Glyph` con
geometría controlada y verificamos la salida de cada componente. Esto prueba
la lógica de reconstrucción sin depender de generar PDFs (rápido, reproducible,
sin red ni dependencias pesadas).

Cubre los fixes de fidelidad para uso académico/IA:
  1. Integridad de caracteres   — cero control/PUA/replacement/desconocidos
  2. Recuperación de espacios    — fin de "andthesecondindicates"
  3. Fracciones apiladas         — \\frac{}{} con guarda anti-falso-positivo
  4. De-fusión de columnas       — fin de ecuaciones falsas (=34x1)
  5. Matrices                    — \\begin{bmatrix} en vez de 1129
  6. No-regresión sub/superíndice

Ejecutar:  python -X utf8 tests/stem_fixtures/test_stem_fidelity.py
(o vía pytest: pytest tests/stem_fixtures/test_stem_fidelity.py)
=============================================================================
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND.resolve()))

import stem_engine as se  # noqa: E402
from stem_engine import _Glyph  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Helper: construir glifos con geometría controlada
# ─────────────────────────────────────────────────────────────────────────────

def g(char: str, x0: float, x1: float, y: float = 100.0,
      size: float = 10.0, font: str = "TestFont") -> _Glyph:
    """Crea un _Glyph. y = baseline (origin_y); bbox vertical aproximado."""
    return _Glyph(
        char=char, size=size, origin_y=y, origin_x=x0,
        bbox_top=y - size, font=font, bbox_x0=x0, bbox_x1=x1,
    )


def line(*glyphs: _Glyph) -> list[_Glyph]:
    return list(glyphs)


REPLACEMENT = chr(0xFFFD)   # �  mojibake irrecuperable
NONCHAR = chr(0xFFFF)       #    noncharacter
PUA = chr(0xE000)           #    Private-Use (fuente rota no recuperada)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Integridad de caracteres
# ─────────────────────────────────────────────────────────────────────────────

def test_charset_strips_control_and_pua():
    # C0 control (\x02..\x1f) se elimina de forma fiable.
    assert se.normalize_unicode("a\x02b\x1fc") == "abc"
    # Replacement, noncharacter y Private-Use NUNCA deben sobrevivir.
    out = se.normalize_unicode("x" + REPLACEMENT + "y" + NONCHAR + "z" + PUA + "w")
    assert out == "xyzw", repr(out)
    # Invariante global: tras normalizar, el audit sale limpio aunque la entrada
    # tenga de todo (un \x80 puede quedar como € vía ftfy — aceptable: es un
    # símbolo conocido, NO un carácter de control/desconocido).
    audit = se.audit_charset(se.normalize_unicode("a\x02b\x80c" + REPLACEMENT + "d"))
    assert audit["clean"], audit
    print("  ✓ C0/replacement/noncharacter/PUA eliminados; audit final limpio")


def test_charset_preserves_real_math():
    math = "∫ α β ≤ ≥ ≠ ± − × ÷ ∞ ∑ ∂ ∇ ℏ"
    out = se.normalize_unicode(math)
    for sym in "∫αβ≤≥≠±−×÷∞∑∂∇ℏ":
        assert sym in out, f"perdió {sym!r}: {out!r}"
    assert se.audit_charset(out)["clean"]
    print("  ✓ símbolos matemáticos reales preservados y auditados limpios")


def test_charset_ligatures_and_soft_hyphen():
    out = se.normalize_unicode("deﬁnes ﬀord conti­nuity")
    assert "defines" in out and "fford" in out
    assert "­" not in out
    print("  ✓ ligaduras expandidas y soft-hyphen eliminado")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Recuperación de espacios posicionales
# ─────────────────────────────────────────────────────────────────────────────

def test_word_space_recovery():
    # "no" pegado, luego hueco > 0.18em (=1.8) hacia "sol"
    gl = line(
        g("n", 0, 6), g("o", 6, 12),
        g("s", 14, 20), g("o", 20, 25), g("l", 25, 27),
    )
    out = se._wrap_runs(gl)
    assert out == "no sol", out
    print(f"  ✓ espacio recuperado por hueco bbox → {out!r}")


def test_no_spurious_space_within_word():
    # "rn" con hueco intra-letra pequeño (<1.8) → NO debe insertar espacio
    gl = line(g("r", 0, 5), g("n", 5.5, 10.5))
    out = se._wrap_runs(gl)
    assert out == "rn", out
    print("  ✓ sin espacio espurio dentro de palabra → 'rn'")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Fracciones apiladas (con guarda anti falso-positivo)
# ─────────────────────────────────────────────────────────────────────────────

def test_stacked_fraction_no_base():
    # "= 4/3": '=' body (size10), '4' super y '3' sub apilados (size7, misma X)
    gl = line(
        g("=", 0, 6, y=100, size=10),
        g("4", 10, 16, y=94, size=7),   # numerador: baseline más arriba (y menor)
        g("3", 10, 16, y=106, size=7),  # denominador: baseline más abajo (y mayor)
    )
    # body_size = tamaño de cuerpo del documento (lo que _format_line le pasa en
    # producción). Sin él, la moda por-línea elegiría 7pt (2 glifos pequeños vs 1
    # de cuerpo) y aplanaría la fracción a "= 43".
    out = se._wrap_runs(gl, body_size=10.0)
    assert "\\frac{4}{3}" in out, out
    print(f"  ✓ fracción sin base → {out!r}")


def test_index_not_fraction():
    # "x_i^2": base alfanumérica 'x' → NO es fracción aunque i/2 se apilen
    gl = line(
        g("x", 0, 6, y=100, size=10),
        g("2", 7, 11, y=94, size=7),   # super
        g("i", 7, 11, y=106, size=7),  # sub
    )
    out = se._wrap_runs(gl, body_size=10.0)
    assert "\\frac" not in out, f"falso positivo de fracción: {out!r}"
    assert "_i" in out and "^2" in out, out
    print(f"  ✓ índice con base NO se confunde con fracción → {out!r}")


def test_body_size_anchor_on_sparse_line():
    """
    Línea matemática ESCASA: '=' de cuerpo (10pt) + numerador/denominador
    pequeños (7pt) apilados. Hay MÁS glifos pequeños que de cuerpo, así que la
    moda local por-línea elige 7pt y aplana la fracción.

    El ancla al body_size del documento (lo que _format_line pasa en producción)
    restaura el baseline correcto. Verificamos AMBOS regímenes para fijar el fix.
    """
    gl = line(
        g("=", 0, 6, y=100, size=10),
        g("4", 10, 16, y=94, size=7),
        g("3", 10, 16, y=106, size=7),
    )
    # Sin ancla: la moda local (7pt, 2 vs 1) aplana → no hay fracción.
    no_anchor = se._wrap_runs(gl)
    assert "\\frac" not in no_anchor, (
        f"sin body_size la moda local no debería detectar fracción: {no_anchor!r}"
    )
    # Con ancla: el '=' a 10pt fija el cuerpo → 4/3 son super/sub → fracción.
    anchored = se._wrap_runs(gl, body_size=10.0)
    assert "\\frac{4}{3}" in anchored, anchored
    print(f"  ✓ ancla body_size rescata fracción escasa: {no_anchor!r} → {anchored!r}")


def test_body_size_anchor_ignored_when_no_body_glyph():
    """
    Línea ENTERAMENTE pequeña (nota al pie a 8pt) con body_size del doc = 10pt.
    NINGÚN glifo llega al cuerpo → el ancla NO debe aplicarse, o toda la nota se
    marcaría como superíndice. El texto debe salir como cuerpo normal.
    """
    gl = line(g("n", 0, 4, y=100, size=8), g("o", 4, 8, y=100, size=8),
              g("t", 8, 11, y=100, size=8), g("e", 11, 15, y=100, size=8))
    out = se._wrap_runs(gl, body_size=10.0)
    assert out == "note", f"nota al pie no debe volverse super/subíndice: {out!r}"
    print(f"  ✓ línea toda-pequeña sin glifo de cuerpo → texto plano {out!r}")


def test_body_size_anchor_does_not_lower_heading():
    """
    GUARDA DEL FIX: el ancla SOLO SUBE line_size, nunca lo baja. Una línea de
    HEADING (todo a 14pt) con body_size=10 NO debe anclarse a 10 — hacerlo
    encogería space_gap (10·0.18=1.8 < 14·0.18=2.52) e insertaría un espacio
    espurio dentro de la palabra del título.

    'Hi' con hueco intra-letra de 2.0px: queda bajo 2.52 (correcto, line_size=14)
    pero superaría 1.8 si el ancla bajara mal a 10.
    """
    gl = line(g("H", 0, 8, y=100, size=14), g("i", 10, 14, y=100, size=14))
    out = se._wrap_runs(gl, body_size=10.0)
    assert out == "Hi", (
        f"ancla bajó line_size en un heading e insertó espacio espurio: {out!r}"
    )
    print(f"  ✓ ancla NO baja line_size en heading → {out!r} (sin espacio espurio)")


# ─────────────────────────────────────────────────────────────────────────────
# 4. De-fusión de columnas físicas
# ─────────────────────────────────────────────────────────────────────────────

def test_column_split():
    # Col-izq "AB" + gutter grande (>4em=40) + col-der "CD"
    gl = line(
        g("A", 0, 6), g("B", 6, 12),
        g("C", 60, 66), g("D", 66, 72),
    )
    segs = se._split_columns(gl)
    assert len(segs) == 2, f"esperaba 2 columnas, dio {len(segs)}"
    assert se._wrap_runs(segs[0]) == "AB"
    assert se._wrap_runs(segs[1]) == "CD"
    print("  ✓ dos columnas físicas separadas → ['AB', 'CD']")


def test_no_split_normal_spacing():
    gl = line(g("A", 0, 6), g("B", 8, 14), g("C", 16, 22))
    segs = se._split_columns(gl)
    assert len(segs) == 1, f"no debía partir: {len(segs)}"
    print("  ✓ espaciado normal NO se parte")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Matrices → \begin{bmatrix}
# ─────────────────────────────────────────────────────────────────────────────

def test_matrix_block():
    lines = [
        line(g("⎡", 0, 8, size=14)),                     # ⎡ corchete apertura
        line(g("1", 2, 8), g("2", 22, 28), g("3", 42, 48)),   # fila 1 (gaps>9)
        line(g("4", 2, 8), g("5", 22, 28), g("6", 42, 48)),   # fila 2
        line(g("⎣", 0, 8, size=14)),                     # ⎣ corchete cierre
    ]
    out = "\n".join(se._render_page(lines, body_size=10.0))
    assert "\\begin{bmatrix}" in out and "\\end{bmatrix}" in out, out
    assert "1 & 2 & 3" in out, out
    assert "4 & 5 & 6" in out, out
    assert "\\\\" in out, out
    print("  ✓ bloque de corchetes → \\begin{bmatrix} con celdas y filas")


def test_no_matrix_without_brackets():
    # Filas de números SIN piezas de corchete → NO se envuelve como matriz
    lines = [
        line(g("1", 2, 8), g("2", 22, 28)),
        line(g("4", 2, 8), g("5", 22, 28)),
    ]
    out = "\n".join(se._render_page(lines, body_size=10.0))
    assert "bmatrix" not in out, out
    print("  ✓ sin piezas de corchete NO se inventa matriz")


# ─────────────────────────────────────────────────────────────────────────────
# 6. No-regresión: sub/superíndices clásicos
# ─────────────────────────────────────────────────────────────────────────────

def test_superscript_basic():
    gl = line(g("x", 0, 6, size=10), g("2", 6, 10, y=94, size=7))
    assert se._wrap_runs(gl) == "x^2"
    print("  ✓ x² → 'x^2'")


def test_subscript_basic():
    gl = line(g("H", 0, 7, size=10), g("2", 7, 11, y=106, size=7),
              g("O", 11, 18, size=10))
    assert se._wrap_runs(gl) == "H_2O"
    print("  ✓ H₂O → 'H_2O'")


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    tests = [
        ("Integridad: control/PUA",        test_charset_strips_control_and_pua),
        ("Integridad: math preservado",    test_charset_preserves_real_math),
        ("Integridad: ligaduras",          test_charset_ligatures_and_soft_hyphen),
        ("Espacios: recuperación",         test_word_space_recovery),
        ("Espacios: sin espurios",         test_no_spurious_space_within_word),
        ("Fracción: sin base",             test_stacked_fraction_no_base),
        ("Fracción: índice no es frac",    test_index_not_fraction),
        ("Ancla: fracción escasa",         test_body_size_anchor_on_sparse_line),
        ("Ancla: nota al pie ignorada",    test_body_size_anchor_ignored_when_no_body_glyph),
        ("Ancla: no baja heading",         test_body_size_anchor_does_not_lower_heading),
        ("Columnas: split",                test_column_split),
        ("Columnas: no split normal",      test_no_split_normal_spacing),
        ("Matriz: bmatrix",                test_matrix_block),
        ("Matriz: no inventa",             test_no_matrix_without_brackets),
        ("Regresión: superíndice",         test_superscript_basic),
        ("Regresión: subíndice",           test_subscript_basic),
    ]
    print("═" * 64)
    print("QA fidelidad matemática — stem_engine")
    print("═" * 64)
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"  ✗ FALLO [{name}]: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ✗ ERROR [{name}]: {type(exc).__name__}: {exc}")
    print("═" * 64)
    if failed:
        print(f"✗ {failed}/{len(tests)} tests fallaron")
        return 1
    print(f"✓ {len(tests)}/{len(tests)} tests pasaron")
    return 0


if __name__ == "__main__":
    sys.exit(main())
