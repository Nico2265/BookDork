"""
Tests unitarios de font_table_recovery.py  (stdlib unittest, sin PDF).

Ejecutar:
    set PYTHONIOENCODING=utf-8
    python -m unittest tests.test_font_table_recovery -v
o bien:
    python -X utf8 tests/test_font_table_recovery.py

Cubre:
  * canonical_family: subset prefix + sufijo de CMap.
  * load_table: compilación hex→int, robustez ante archivo ausente/corrupto,
    descarte de entradas sin unicode.
  * GlyphTableRecovery.resolve: acierto, abstención por familia y por codepoint,
    estadísticas, covers().
  * EL REGISTRO ENVIADO (font_glyph_table.json): valida las entradas verificadas
    y que el glifo 0x60 (abstención deliberada) NO esté presente.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(_BACKEND))

import font_table_recovery as ftr  # noqa: E402


class TestCanonicalFamily(unittest.TestCase):
    def test_subset_prefix(self):
        self.assertEqual(ftr.canonical_family("KKBOMG+MathematicalPi-Five"),
                         "MathematicalPi-Five")

    def test_cmap_suffix(self):
        self.assertEqual(ftr.canonical_family("MathematicalPi-Five-Identity-H"),
                         "MathematicalPi-Five")

    def test_both(self):
        self.assertEqual(ftr.canonical_family("ABCDEF+Foo-Identity-V"), "Foo")

    def test_no_overstrip(self):
        self.assertEqual(ftr.canonical_family("TimesLTStd-Bold"), "TimesLTStd-Bold")

    def test_empty(self):
        self.assertEqual(ftr.canonical_family(""), "")
        self.assertEqual(ftr.canonical_family(None), "")


class TestLoadTable(unittest.TestCase):
    def _write(self, obj) -> Path:
        f = Path(tempfile.mkdtemp()) / "t.json"
        f.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return f

    def test_compiles_hex_to_int(self):
        t = self._write({"fonts": {"FooFont": {"006D": {"unicode": "="}}}})
        table = ftr.load_table(t)
        self.assertEqual(table["FooFont"][0x6D], "=")

    def test_canonicalizes_family_key(self):
        t = self._write({"fonts": {"ABCDEF+FooFont-Identity-H": {"0040": {"unicode": "∞"}}}})
        table = ftr.load_table(t)
        self.assertIn("FooFont", table)
        self.assertEqual(table["FooFont"][0x40], "∞")

    def test_skips_empty_unicode(self):
        t = self._write({"fonts": {"FooFont": {"0060": {"unicode": ""},
                                               "006D": {"unicode": "="}}}})
        table = ftr.load_table(t)
        self.assertNotIn(0x60, table["FooFont"])
        self.assertIn(0x6D, table["FooFont"])

    def test_skips_invalid_hex(self):
        t = self._write({"fonts": {"FooFont": {"ZZZZ": {"unicode": "x"},
                                               "006D": {"unicode": "="}}}})
        table = ftr.load_table(t)
        self.assertEqual(list(table["FooFont"]), [0x6D])

    def test_missing_file_returns_empty(self):
        self.assertEqual(ftr.load_table(Path("no_existe_xyz.json")), {})

    def test_corrupt_file_returns_empty(self):
        f = Path(tempfile.mkdtemp()) / "bad.json"
        f.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(ftr.load_table(f), {})


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.rec = ftr.GlyphTableRecovery(
            table={"MathematicalPi-Five": {0x6D: "=", 0x6F: "≠"}})

    def test_hit(self):
        self.assertEqual(self.rec.resolve("KKBOMG+MathematicalPi-Five", 0x6D), "=")

    def test_hit_with_identity_suffix(self):
        self.assertEqual(self.rec.resolve("MathematicalPi-Five-Identity-H", 0x6F), "≠")

    def test_abstains_unknown_family(self):
        self.assertIsNone(self.rec.resolve("Times-Roman", 0x6D))

    def test_abstains_unknown_codepoint(self):
        self.assertIsNone(self.rec.resolve("MathematicalPi-Five", 0x41))

    def test_stats(self):
        self.rec.resolve("MathematicalPi-Five", 0x6D)   # hit
        self.rec.resolve("Times-Roman", 0x6D)           # abstain
        s = self.rec.stats()
        self.assertEqual(s["resolved"], 1)
        self.assertEqual(s["abstained"], 1)
        self.assertEqual(s["resolve_calls"], 2)

    def test_covers(self):
        self.assertTrue(self.rec.covers("ABCDEF+MathematicalPi-Five"))
        self.assertFalse(self.rec.covers("Times-Roman"))

    def test_empty_table_always_abstains(self):
        rec = ftr.GlyphTableRecovery(table={})
        self.assertIsNone(rec.resolve("MathematicalPi-Five", 0x6D))


class TestShippedRegistry(unittest.TestCase):
    """Valida el contenido de font_glyph_table.json (el que se envía)."""

    @classmethod
    def setUpClass(cls):
        cls.table = ftr.load_table()   # ruta por defecto = el JSON real

    def test_loads_nonempty(self):
        self.assertGreater(len(self.table), 0)

    def test_verified_entries(self):
        expected = {
            ("MathematicalPi-Five", 0x6D): "=",
            ("MathematicalPi-Five", 0x6F): "≠",
            ("MathematicalPi-One", 0x40): "∞",
            ("MathematicalPi-One", 0x24): "Δ",
            ("MathematicalPi-One", 0x76): "≤",
            ("MathematicalPi-One", 0x77): "≥",
            ("MathematicalPi-Three", 0x46): "[",
            ("MathematicalPi-Three", 0x47): "]",
            ("MathematicalPi-Three", 0x48): "{",
            ("MathematicalPi-Three", 0x4A): "}",
            ("MathematicalPi-Three", 0x4F): "∑",
            ("MathematicalPi-Three", 0x55): "|",
            ("MathematicalPi-Three", 0x59): "/",
            # Fase 3
            ("MathematicalPi-Three", 0x25): "∑",
            ("MathematicalPi-Three", 0x79): "≈",
            ("Grk", 0x29): "π",
            ("Grk", 0x2E): "θ",
            ("Grk", 0x26): "μ",
            ("Grk", 0x2B): "ρ",
            ("ProgressivePi", 0x3A): "→",
            # Fase 4
            ("Grk", 0x25): "λ",
            ("Grk", 0x2C): "σ",
            ("MathematicalPi-Three", 0x56): "⟦",
            ("MathematicalPi-Three", 0x42): "⟧",
            ("MathematicalPiOneOblique", 0x70): "π",
        }
        for (fam, cp), u in expected.items():
            self.assertEqual(self.table.get(fam, {}).get(cp), u,
                             f"{fam} 0x{cp:02X} debería ser {u!r}")

    def test_abstained_entries_absent(self):
        # Glifos donde render y contexto NO coincidían, eran decorativos o
        # ambiguos → abstención deliberada (marcador honesto > mapeo adivinado).
        abstained = [
            ("MathematicalPi-One", 0x60),    # ∞? aislado, posible duplicado
            ("MathematicalPi-One", 0x23),    # render τ vs ctx θ → conflicto
            ("MathematicalPi-One", 0x36),    # render ρ vs ctx 'ohms'→Ω → conflicto
            ("MathematicalPi-One", 0x21),    # render vacío
            ("WWDOC15", 0x52),               # render | vs ctx ∫ → conflicto
            ("Universal-NewswithCommPi", 0x3B),  # bullet decorativo
            ("MathematicalPiOneOblique", 0x61),  # α/itálica ambigua (cluster mixto)
            ("MathematicalPiOneOblique", 0x66),  # 'f' itálica REAL (función f) — no tocar
            ("MathematicalPi-Three", 0x22),  # | vs ′ ambiguo
        ]
        for fam, cp in abstained:
            self.assertNotIn(cp, self.table.get(fam, {}),
                             f"{fam} 0x{cp:02X} NO debería estar (abstención)")

    def test_resolve_via_shipped(self):
        rec = ftr.GlyphTableRecovery()
        self.assertEqual(rec.resolve("KKCAOO+MathematicalPi-Five", 0x6D), "=")


if __name__ == "__main__":
    unittest.main(verbosity=2)
