"""
Tests unitarios de glyph_name_recovery.py  (stdlib unittest, sin dependencias).

Ejecutar:
    set PYTHONIOENCODING=utf-8
    python -m unittest tests.test_glyph_name_recovery -v
o bien:
    python tests/test_glyph_name_recovery.py

Cubre:
  * resolve_glyph_name: AGL estándar, corrección del bug uXXXXXX de MuPDF,
    ligaduras, abstención en nombres sin semántica, suplemento.
  * parse_differences: semántica de auto-incremento del array PDF.
  * GlyphNameRecovery end-to-end sobre un PDF sintético con /Differences
    (incluye el caso 𝛼=u1D6FC que MuPDF emite mal y este canal recupera).
"""
import sys
import unittest
from pathlib import Path

# Permite importar el módulo desde "backend" sin instalar el paquete.
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(_BACKEND))

import fitz  # noqa: E402
import glyph_name_recovery as gnr  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Resolución de nombres (lógica pura, sin PDF)
# ─────────────────────────────────────────────────────────────────────────────
class TestResolveGlyphName(unittest.TestCase):

    def test_agl_standard_names(self):
        for name, expected in [
            ("summation", "∑"),
            ("integral", "∫"),
            ("alpha", "α"),
            ("partialdiff", "∂"),
            ("gradient", "∇"),
            ("plusminus", "±"),
            ("infinity", "∞"),
        ]:
            res = gnr.resolve_glyph_name(name)
            self.assertIsNotNone(res, f"{name} debería resolver")
            self.assertEqual(res.char, expected, f"{name} → {res.char!r} ≠ {expected!r}")
            self.assertEqual(res.source, gnr.SOURCE_AGL)

    def test_leading_slash_is_stripped(self):
        self.assertEqual(gnr.resolve_glyph_name("/alpha").char, "α")

    def test_uXXXXXX_mupdf_truncation_bug_is_fixed(self):
        # MuPDF emite u1D6FC como U+D6FC (trunca a 4 hex). El canal lo recupera.
        res = gnr.resolve_glyph_name("u1D6FC")
        self.assertIsNotNone(res)
        self.assertEqual(res.char, "\U0001D6FC")  # 𝛼 Mathematical Italic Small Alpha
        self.assertEqual(ord(res.char), 0x1D6FC)

    def test_uniXXXX_form(self):
        self.assertEqual(gnr.resolve_glyph_name("uni2211").char, "∑")

    def test_dotted_suffix_name(self):
        # 'alpha.sc' / 'uni03B1.alt' → AGL toma la parte antes del punto.
        self.assertEqual(gnr.resolve_glyph_name("uni03B1.alt").char, "α")

    def test_ligature_name_multichar(self):
        res = gnr.resolve_glyph_name("f_f")
        self.assertIsNotNone(res)
        self.assertEqual(res.char, "ff")  # ligadura → multi-carácter

    def test_abstains_on_semantic_void(self):
        for name in [".notdef", ".null", "g42", "glyph7", "index12", "cid900", ""]:
            self.assertIsNone(gnr.resolve_glyph_name(name), f"{name!r} debía abstenerse")

    def test_abstains_on_unknown_custom_name(self):
        # H11001 no está en AGL → sin suplemento, abstención.
        self.assertIsNone(gnr.resolve_glyph_name("H11001"))

    def test_supplement_resolves_custom_name(self):
        sup = {"H11001": "+", "H11002": "−"}
        res = gnr.resolve_glyph_name("H11001", supplement=sup)
        self.assertIsNotNone(res)
        self.assertEqual(res.char, "+")
        self.assertEqual(res.source, gnr.SOURCE_SUPPLEMENT)

    def test_agl_takes_priority_over_supplement(self):
        # Aunque el suplemento intente redefinir 'alpha', AGL (canónico) gana.
        sup = {"alpha": "X"}
        self.assertEqual(gnr.resolve_glyph_name("alpha", supplement=sup).char, "α")


# ─────────────────────────────────────────────────────────────────────────────
# Tokenizador del array /Differences
# ─────────────────────────────────────────────────────────────────────────────
class TestParseDifferences(unittest.TestCase):

    def test_basic(self):
        self.assertEqual(
            gnr.parse_differences("[ 2 /summation 3 /integral ]"),
            {2: "summation", 3: "integral"},
        )

    def test_auto_increment_after_single_int(self):
        # Un entero fija el código; los nombres siguientes incrementan.
        self.assertEqual(
            gnr.parse_differences("[ 2 /summation /integral /alpha ]"),
            {2: "summation", 3: "integral", 4: "alpha"},
        )

    def test_compact_no_spaces(self):
        # MuPDF devuelve el array compacto: '[2/summation 3/integral]'
        self.assertEqual(
            gnr.parse_differences("[2/summation 3/integral]"),
            {2: "summation", 3: "integral"},
        )

    def test_multiple_runs(self):
        self.assertEqual(
            gnr.parse_differences("[ 2 /a /b 10 /c ]"),
            {2: "a", 3: "b", 10: "c"},
        )

    def test_names_with_dots_and_digits(self):
        self.assertEqual(
            gnr.parse_differences("[ 2 /uni03B1.alt 3 /H11001 ]"),
            {2: "uni03B1.alt", 3: "H11001"},
        )

    def test_empty(self):
        self.assertEqual(gnr.parse_differences(""), {})
        self.assertEqual(gnr.parse_differences("[ ]"), {})

    def test_names_before_any_int_are_ignored(self):
        # Sin un código inicial no se puede asignar; PDF válido siempre lo trae.
        self.assertEqual(gnr.parse_differences("[ /a /b ]"), {})


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end sobre PDF sintético con /Differences (incluye el caso 𝛼=u1D6FC)
# ─────────────────────────────────────────────────────────────────────────────
def _build_differences_pdf(path: Path) -> None:
    """
    PDF con una fuente simple cuyo /Differences declara:
        2 /summation   (AGL, MuPDF también lo resuelve)
        3 /u1D6FC      (𝛼; MuPDF lo emite MAL, el canal lo recupera)
        4 /H11001      (custom no-AGL; abstención salvo suplemento)
    Sin ToUnicode → reproduce el modo de fallo de las fuentes math rotas.
    """
    doc = fitz.open()
    page = doc.new_page(width=300, height=200)
    enc = doc.get_new_xref()
    doc.update_object(enc, "<< /Type /Encoding /Differences "
                           "[ 2 /summation 3 /u1D6FC 4 /H11001 ] >>")
    fx = doc.get_new_xref()
    doc.update_object(fx, f"<< /Type /Font /Subtype /Type1 "
                          f"/BaseFont /ABCDEF+MathematicalPi-One /Encoding {enc} 0 R >>")
    cx = doc.get_new_xref()
    doc.update_object(cx, "<< >>")
    doc.update_stream(cx, b"BT /F1 24 Tf 50 100 Td (\\002\\003\\004) Tj ET")
    doc.xref_set_key(page.xref, "Contents", f"{cx} 0 R")
    doc.xref_set_key(page.xref, "Resources", f"<< /Font << /F1 {fx} 0 R >> >>")
    doc.save(str(path))
    doc.close()


class TestEndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.pdf_path = Path(__file__).with_name("_test_diff_fixture.pdf")
        _build_differences_pdf(cls.pdf_path)
        cls.doc = fitz.open(str(cls.pdf_path))
        cls.font_xref = cls.doc[0].get_fonts(full=True)[0][0]

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()
        try:
            cls.pdf_path.unlink()
        except OSError:
            pass

    def test_font_map_extracted_from_differences(self):
        rec = gnr.GlyphNameRecovery(self.doc, supplement={})
        diag = rec.font_diagnostics(self.font_xref)
        self.assertEqual(diag.source, "differences")
        self.assertFalse(diag.has_tounicode)
        self.assertEqual(diag.code_to_name,
                         {2: "summation", 3: "u1D6FC", 4: "H11001"})

    def test_resolves_standard_and_recovers_mupdf_bug(self):
        rec = gnr.GlyphNameRecovery(self.doc, supplement={})
        self.assertEqual(rec.resolve(self.font_xref, 2).char, "∑")
        # El caso clave: 𝛼 que MuPDF emite truncado, recuperado correctamente.
        res = rec.resolve(self.font_xref, 3)
        self.assertEqual(ord(res.char), 0x1D6FC)
        self.assertEqual(res.source, gnr.SOURCE_AGL)

    def test_abstains_and_harvests_unknown_name(self):
        rec = gnr.GlyphNameRecovery(self.doc, supplement={})
        self.assertIsNone(rec.resolve(self.font_xref, 4))     # H11001 desconocido
        self.assertEqual(rec.unresolved_names().get("H11001"), 1)
        self.assertEqual(rec.stats()["abstained_unknown"], 1)

    def test_supplement_closes_the_gap(self):
        rec = gnr.GlyphNameRecovery(self.doc, supplement={"H11001": "+"})
        res = rec.resolve(self.font_xref, 4)
        self.assertIsNotNone(res)
        self.assertEqual(res.char, "+")
        self.assertEqual(res.source, gnr.SOURCE_SUPPLEMENT)

    def test_abstains_when_code_has_no_name(self):
        rec = gnr.GlyphNameRecovery(self.doc, supplement={})
        self.assertIsNone(rec.resolve(self.font_xref, 99))    # código sin declarar
        self.assertEqual(rec.stats()["abstained_no_name"], 1)

    def test_stats_breakdown(self):
        rec = gnr.GlyphNameRecovery(self.doc, supplement={})
        for code in (2, 3, 4, 99):
            rec.resolve(self.font_xref, code)
        s = rec.stats()
        self.assertEqual(s["resolved_agl"], 2)        # summation + u1D6FC
        self.assertEqual(s["abstained_unknown"], 1)   # H11001
        self.assertEqual(s["abstained_no_name"], 1)   # code 99
        self.assertEqual(s["fonts_with_names"], 1)


class TestNameFor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pdf_path = Path(__file__).with_name("_test_namefor.pdf")
        _build_differences_pdf(cls.pdf_path)
        cls.doc = fitz.open(str(cls.pdf_path))
        cls.font_xref = cls.doc[0].get_fonts(full=True)[0][0]

    @classmethod
    def tearDownClass(cls):
        cls.doc.close()
        try:
            cls.pdf_path.unlink()
        except OSError:
            pass

    def test_name_for_returns_declared_name(self):
        rec = gnr.GlyphNameRecovery(self.doc, supplement={})
        self.assertEqual(rec.name_for(self.font_xref, 4), "H11001")
        self.assertEqual(rec.name_for(self.font_xref, 2), "summation")

    def test_name_for_none_when_no_name(self):
        rec = gnr.GlyphNameRecovery(self.doc, supplement={})
        self.assertIsNone(rec.name_for(self.font_xref, 99))


# ─────────────────────────────────────────────────────────────────────────────
# Marcador de abstención visible en stem_engine (anti-borrado silencioso)
# ─────────────────────────────────────────────────────────────────────────────
def _build_broken_pdf(path: Path, basefont: str) -> None:
    """
    PDF con /Differences [2 /summation 3 /H99999]:
      code 2 (/summation) → MuPDF lo resuelve a ∑ (no es glifo roto)
      code 3 (/H99999)    → AGL no lo conoce → MuPDF emite \\x03 (glifo roto)
    El basefont decide si la fuente se considera 'rota' (MathematicalPi-One sí).
    """
    doc = fitz.open()
    page = doc.new_page(width=300, height=200)
    enc = doc.get_new_xref()
    doc.update_object(enc, "<< /Type /Encoding /Differences [ 2 /summation 3 /H99999 ] >>")
    fx = doc.get_new_xref()
    doc.update_object(fx, f"<< /Type /Font /Subtype /Type1 "
                          f"/BaseFont /{basefont} /Encoding {enc} 0 R >>")
    cx = doc.get_new_xref()
    doc.update_object(cx, "<< >>")
    doc.update_stream(cx, b"BT /F1 24 Tf 50 100 Td (\\002\\003) Tj ET")
    doc.xref_set_key(page.xref, "Contents", f"{cx} 0 R")
    doc.xref_set_key(page.xref, "Resources", f"<< /Font << /F1 {fx} 0 R >> >>")
    doc.save(str(path))
    doc.close()


class TestUnresolvedMarker(unittest.TestCase):
    """El glifo roto irrecuperable se marca ⟦?nombre⟧, no se borra en silencio."""

    def _chars_from(self, pdf_path, basename_for_recovery):
        import stem_engine as se
        doc = fitz.open(str(pdf_path))
        try:
            rec = gnr.GlyphNameRecovery(doc, supplement={})  # suplemento vacío
            lines = se._extract_glyphs(doc[0], font_recovery=None, glyph_name_recovery=rec)
            return "".join(g.char for line in lines for g in line)
        finally:
            doc.close()

    def test_unknown_glyph_from_broken_font_is_marked(self):
        p = Path(__file__).with_name("_test_marker_broken.pdf")
        _build_broken_pdf(p, "ABCDEF+MathematicalPi-One")
        try:
            text = self._chars_from(p, "MathematicalPi-One")
            self.assertIn("⟦?H99999⟧", text)   # marcado, diagnóstico, NO borrado
            self.assertIn("∑", text)             # el AGL-resoluble sigue intacto
            self.assertNotIn("\x03", text)       # no queda código crudo
        finally:
            p.unlink(missing_ok=True)

    def test_raw_glyph_from_healthy_font_is_not_marked(self):
        # Misma estructura pero fuente NO rota → no se marca (sería ruido genuino,
        # _sanitize_charset lo limpia después). Evita falsos marcadores.
        p = Path(__file__).with_name("_test_marker_healthy.pdf")
        _build_broken_pdf(p, "Helvetica")
        try:
            text = self._chars_from(p, "Helvetica")
            self.assertNotIn("⟦?", text)
        finally:
            p.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# REGRESIÓN — blinda los errores YA parchados para que no puedan reaparecer
# ─────────────────────────────────────────────────────────────────────────────
def _build_mathpi_pdf(path: Path) -> None:
    """
    PDF que reproduce el modo de fallo REAL de Zill: fuente MathematicalPi-One
    con /Differences de nombres Adobe Math-Pi (no-AGL) y sin ToUnicode.
      code 2 /H11005 (=)  3 /H11001 (+)  4 /H11002 (−)  5 /H9024 (Ω)
      code 6 /H99999      → desconocido (debe salir como marcador, no borrado)
    MuPDF emite \\x02..\\x06 crudos (ninguno es AGL) → los resuelve el canal.
    """
    doc = fitz.open()
    page = doc.new_page(width=320, height=200)
    enc = doc.get_new_xref()
    doc.update_object(enc, "<< /Type /Encoding /Differences "
                           "[ 2 /H11005 3 /H11001 4 /H11002 5 /H9024 6 /H99999 ] >>")
    fx = doc.get_new_xref()
    doc.update_object(fx, f"<< /Type /Font /Subtype /Type1 "
                          f"/BaseFont /PBOFBK+MathematicalPi-One /Encoding {enc} 0 R >>")
    cx = doc.get_new_xref()
    doc.update_object(cx, "<< >>")
    doc.update_stream(cx, b"BT /F1 24 Tf 40 100 Td (x\\002\\0035\\004\\005\\006) Tj ET")
    doc.xref_set_key(page.xref, "Contents", f"{cx} 0 R")
    doc.xref_set_key(page.xref, "Resources", f"<< /Font << /F1 {fx} 0 R >> >>")
    doc.save(str(path))
    doc.close()


class TestRegressionPatchedErrors(unittest.TestCase):
    """
    Estos tests fallan si reaparece alguno de los bugs ya corregidos:
      (1) `=` (H11005) convertido a `⇋` por el matcher visual.
      (2) Borrado SILENCIOSO de un glifo roto irrecuperable.
      (3) Suplemento sin las entradas críticas verificadas.
    """

    HARPOONS = "⇋⇌⇆⇄"  # símbolos de arpón con los que el visual confundía '='

    @classmethod
    def setUpClass(cls):
        cls.pdf = Path(__file__).with_name("_test_regression_mathpi.pdf")
        _build_mathpi_pdf(cls.pdf)
        cls.buf = cls.pdf.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.pdf.unlink(missing_ok=True)

    def _convert(self, disable_visual=False):
        import stem_engine as se
        if disable_visual:
            orig = se._build_font_recovery_if_needed
            se._build_font_recovery_if_needed = lambda doc: None
            try:
                return se.convert_pdf_stem(self.buf, "reg.pdf")["markdown"]
            finally:
                se._build_font_recovery_if_needed = orig
        return se.convert_pdf_stem(self.buf, "reg.pdf")["markdown"]

    def test_equals_is_not_turned_into_harpoon(self):
        # EL BUG ORIGINAL: '=' (H11005) → '⇋'. El canal de nombres lo resuelve
        # de forma determinista ANTES del visual, así que esto debe cumplirse
        # incluso con el matcher visual activo.
        md = self._convert(disable_visual=False)
        self.assertIn("=", md, "se perdió el signo igual")
        for h in self.HARPOONS:
            self.assertNotIn(h, md, f"REGRESIÓN: '=' volvió a convertirse en {h!r}")

    def test_operators_recovered_deterministically(self):
        md = self._convert(disable_visual=False)
        for sym in ("+", "−", "Ω"):
            self.assertIn(sym, md, f"operador {sym!r} no recuperado")

    def test_no_raw_control_codes_survive(self):
        md = self._convert(disable_visual=False)
        for cp in (0x02, 0x03, 0x04, 0x05, 0x06):
            self.assertNotIn(chr(cp), md, f"código crudo U+{cp:02X} sobrevivió")

    def test_unknown_glyph_marked_not_silently_deleted(self):
        # Visual desactivado para aislar: el glifo desconocido (H99999) debe
        # quedar como marcador VISIBLE, jamás borrado en silencio.
        md = self._convert(disable_visual=True)
        self.assertIn("⟦?H99999⟧", md,
                      "REGRESIÓN: glifo irrecuperable borrado en silencio en vez de marcado")

    def test_supplement_has_critical_verified_entries(self):
        # Guarda contra edición accidental del JSON que quite operadores clave.
        sup = gnr.load_supplement()
        for name, expected in [("H11005", "="), ("H11001", "+"), ("H11002", "−"),
                               ("H11003", "×"), ("H9024", "Ω"), ("HS11005", "≠")]:
            self.assertEqual(sup.get(name), expected,
                             f"suplemento perdió/cambió {name} (esperado {expected!r})")

    def test_resolver_uses_real_supplement(self):
        sup = gnr.load_supplement()
        self.assertEqual(gnr.resolve_glyph_name("H11005", sup).char, "=")
        self.assertEqual(gnr.resolve_glyph_name("H9024", sup).char, "Ω")


if __name__ == "__main__":
    unittest.main(verbosity=2)
