"""
Tests unitarios de font_harvester.py  (stdlib unittest, sin dependencias).

Ejecutar:
    set PYTHONIOENCODING=utf-8
    python -m unittest tests.test_font_harvester -v
o bien:
    python -X utf8 tests/test_font_harvester.py

Cubre la lógica PURA del harvester (sin abrir PDFs):
  * canonical_font_key: subset prefix + sufijos de CMap.
  * glyph_risk: las 5 categorías de riesgo, espejando el motor.
  * is_structural_suspect: la regla de alta precisión Type0/Identity/no-ToUnicode.
  * zipf_coverage: curva de cobertura y bordes.
  * FontReport / merge / blind_spots / build_inventory end-to-end sintético.
"""
import sys
import unittest
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(_BACKEND))

import font_harvester as fh  # noqa: E402


class TestCanonicalFontKey(unittest.TestCase):
    def test_strips_subset_prefix(self):
        self.assertEqual(fh.canonical_font_key("KKBOMG+MathematicalPi-Five"),
                         "MathematicalPi-Five")

    def test_strips_identity_cmap_suffix(self):
        self.assertEqual(fh.canonical_font_key("MathematicalPi-Five-Identity-H"),
                         "MathematicalPi-Five")

    def test_strips_both(self):
        self.assertEqual(fh.canonical_font_key("ABCDEF+SomeFont-Identity-V"),
                         "SomeFont")

    def test_does_not_overstrip_bold_suffix(self):
        # '-Bold' termina en algo que NO es un sufijo de CMap conocido.
        self.assertEqual(fh.canonical_font_key("ABCDEF+TimesLTStd-Bold"),
                         "TimesLTStd-Bold")

    def test_empty_and_none(self):
        self.assertEqual(fh.canonical_font_key(""), "")
        self.assertEqual(fh.canonical_font_key(None), "")


class TestGlyphRisk(unittest.TestCase):
    def test_printable_ascii_from_suspect_is_silent(self):
        # '=' que sale como 'm' desde una fuente sospechosa no-Grk.
        self.assertEqual(fh.glyph_risk("m", font_is_suspect=True, is_grk=False),
                         fh.SILENT_MISMAP)

    def test_printable_ascii_from_clean_font_is_ok(self):
        self.assertEqual(fh.glyph_risk("m", font_is_suspect=False, is_grk=False),
                         fh.OK)

    def test_grk_alpha_is_recovered(self):
        self.assertEqual(fh.glyph_risk("a", font_is_suspect=True, is_grk=True),
                         fh.RECOVERED_GRK)

    def test_grk_nonalpha_still_silent_if_printable(self):
        # En fuente Grk, un dígito/puntuación imprimible NO lo cubre el mapa
        # fonético (requiere isalpha) → sigue siendo silencioso.
        self.assertEqual(fh.glyph_risk("/", font_is_suspect=True, is_grk=True),
                         fh.SILENT_MISMAP)

    def test_control_from_suspect_is_markered(self):
        self.assertEqual(fh.glyph_risk("\x12", font_is_suspect=True, is_grk=False),
                         fh.MARKERED_CONTROL)

    def test_control_from_clean_is_stray(self):
        self.assertEqual(fh.glyph_risk("\x12", font_is_suspect=False, is_grk=False),
                         fh.STRAY_CONTROL)

    def test_whitespace_is_ok(self):
        self.assertEqual(fh.glyph_risk(" ", font_is_suspect=True, is_grk=False),
                         fh.OK)

    def test_real_unicode_symbol_is_ok(self):
        # Un '∑' ya correcto desde cualquier fuente no es sospechoso.
        self.assertEqual(fh.glyph_risk("∑", font_is_suspect=True, is_grk=False),
                         fh.OK)

    def test_empty_is_ok(self):
        self.assertEqual(fh.glyph_risk("", font_is_suspect=True, is_grk=False),
                         fh.OK)


class TestStructuralSuspect(unittest.TestCase):
    def _meta(self, **kw):
        base = dict(family="X", subtype="Type0", encoding="Identity-H",
                    has_tounicode=False)
        base.update(kw)
        return fh.FontMeta(**base)

    def test_type0_identity_no_tounicode_symbol_like_is_suspect(self):
        # alpha_ratio bajo (predominan símbolos) → sospechosa.
        self.assertTrue(fh.is_structural_suspect(
            self._meta(), emits_printable_ascii=True, alpha_ratio=0.2))

    def test_type0_identity_body_text_not_suspect(self):
        # FALSO POSITIVO a evitar: AGaramond/Times-Bold (Type0/Identity sin
        # ToUnicode) que decodifican prosa legible → alpha_ratio alto → FUERA.
        self.assertFalse(fh.is_structural_suspect(
            self._meta(), emits_printable_ascii=True, alpha_ratio=0.95))

    def test_not_suspect_without_printable(self):
        self.assertFalse(fh.is_structural_suspect(self._meta(), emits_printable_ascii=False))

    def test_not_suspect_with_tounicode(self):
        self.assertFalse(
            fh.is_structural_suspect(self._meta(has_tounicode=True),
                                     emits_printable_ascii=True))

    def test_not_suspect_simple_type1(self):
        m = self._meta(subtype="Type1", encoding="WinAnsiEncoding")
        self.assertFalse(fh.is_structural_suspect(m, emits_printable_ascii=True))

    def test_is_identity_type0_property(self):
        self.assertTrue(self._meta().is_identity_type0)
        self.assertFalse(self._meta(encoding="WinAnsiEncoding").is_identity_type0)


class TestZipfCoverage(unittest.TestCase):
    def test_basic_curve(self):
        # Una fuente domina (100) sobre el resto (10,10,10,10,10) → total 150.
        counts = [100, 10, 10, 10, 10, 10]
        zc = fh.zipf_coverage(counts, fractions=(0.50, 0.95, 1.0))
        self.assertEqual(zc[0.50], 1)        # la grande sola ya cubre 66% ≥ 50%
        self.assertEqual(zc[1.0], 6)

    def test_unordered_input(self):
        self.assertEqual(fh.zipf_coverage([10, 100, 10])[0.50], 1)

    def test_empty(self):
        self.assertEqual(fh.zipf_coverage([])[0.95], 0)

    def test_all_equal(self):
        zc = fh.zipf_coverage([1, 1, 1, 1], fractions=(0.50,))
        self.assertEqual(zc[0.50], 2)        # 2/4 = 50%


class TestFontReportAndMerge(unittest.TestCase):
    def _silent_font(self, fam="MathematicalPi-Five", char="m", cp=0x6D,
                     count=10, book="a.pdf"):
        r = fh.FontReport(family=fam, subtype="Type0", encoding="Identity-H",
                          structural_suspect=True, books={book})
        r.codepoints[cp] = {"char": char, "count": count, "risk": fh.SILENT_MISMAP}
        return r

    def test_counts_aggregate(self):
        r = self._silent_font(count=5)
        r.codepoints[0x10] = {"char": "\x10", "count": 3, "risk": fh.MARKERED_CONTROL}
        self.assertEqual(r.silent_count, 5)
        self.assertEqual(r.control_count, 3)
        self.assertEqual(r.total_glyphs, 8)

    def test_merge_sums_and_unions_books(self):
        corpus = fh.CorpusReport()
        fh._merge_into(corpus, {"MathematicalPi-Five": self._silent_font(count=10, book="a.pdf")})
        fh._merge_into(corpus, {"MathematicalPi-Five": self._silent_font(count=7, book="b.pdf")})
        agg = corpus.fonts["MathematicalPi-Five"]
        self.assertEqual(agg.silent_count, 17)
        self.assertEqual(agg.books, {"a.pdf", "b.pdf"})

    def test_blind_spot_detection(self):
        corpus = fh.CorpusReport()
        # structural pero NO name_pattern_broken → punto ciego.
        r = self._silent_font(fam="WeirdHouseFont")
        r.name_pattern_broken = False
        fh._merge_into(corpus, {"WeirdHouseFont": r})
        # una fuente que el regex SÍ detecta → no es punto ciego.
        r2 = self._silent_font(fam="MathematicalPi-Five")
        r2.name_pattern_broken = True
        fh._merge_into(corpus, {"MathematicalPi-Five": r2})
        blind = {r.family for r in corpus.blind_spots()}
        self.assertEqual(blind, {"WeirdHouseFont"})

    def test_worst_risk_wins_on_merge(self):
        self.assertEqual(fh._worst_risk(fh.OK, fh.SILENT_MISMAP), fh.SILENT_MISMAP)
        self.assertEqual(fh._worst_risk(fh.MARKERED_CONTROL, fh.OK), fh.MARKERED_CONTROL)


class TestBuildInventory(unittest.TestCase):
    def test_inventory_shape_and_worklist(self):
        corpus = fh.CorpusReport()
        corpus.pdfs_ok = 1
        r = fh.FontReport(family="MathematicalPi-Five", subtype="Type0",
                          encoding="Identity-H", structural_suspect=True,
                          books={"stewart.pdf"})
        r.codepoints[0x6D] = {"char": "m", "count": 5428, "risk": fh.SILENT_MISMAP}
        r.codepoints[0x20] = {"char": " ", "count": 1852, "risk": fh.OK}  # se filtra
        corpus.fonts["MathematicalPi-Five"] = r
        inv = fh.build_inventory(corpus)
        self.assertEqual(inv["summary"]["fonts_suspect"], 1)
        self.assertEqual(len(inv["fonts"]), 1)
        font = inv["fonts"][0]
        self.assertEqual(font["family"], "MathematicalPi-Five")
        # solo el glifo silencioso entra al worklist, no el espacio OK.
        self.assertEqual(len(font["glyphs"]), 1)
        g = font["glyphs"][0]
        self.assertEqual(g["codepoint"], "U+006D")
        self.assertEqual(g["emitted_char"], "m")
        self.assertEqual(g["unicode"], "")   # campo a rellenar en curación

    def test_clean_font_excluded(self):
        corpus = fh.CorpusReport()
        r = fh.FontReport(family="Times-Roman", subtype="Type1")
        r.codepoints[0x6D] = {"char": "m", "count": 100, "risk": fh.OK}
        corpus.fonts["Times-Roman"] = r
        inv = fh.build_inventory(corpus)
        self.assertEqual(inv["fonts"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
