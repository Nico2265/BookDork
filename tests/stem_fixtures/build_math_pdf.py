"""
Generador de PDF matemático sintético con ground truth conocido.

Mimica un libro de física/matemáticas LaTeX-generado a nivel glifo:
  - Tamaños de fuente diferenciados para body/super/subíndice
  - Baseline shifts reales (origin.y desplazado)
  - Símbolos Unicode matemáticos en sus puntos de código correctos
  - Ligaduras tipográficas (ﬁ, ﬀ)
  - Palabras partidas con guión cross-line
  - Caracteres especiales que rompen pipelines naïve

Usa Arial (incluye Greek/math básico) para que los glifos Unicode
sobrevivan la creación del PDF; los problemas observados al extraer
son entonces problemas REALES de extracción, no de creación.
"""
from __future__ import annotations

import json
from pathlib import Path

import fitz

HERE = Path(__file__).parent
OUT_PDF = HERE / "math_textbook_sample.pdf"
OUT_GT = HERE / "ground_truth.json"

# Fuentes con cobertura Unicode matemática completa (greek + math operators)
# Cambria Math está embebida en cada instalación de Windows y es la fuente
# de referencia para ecuaciones en Word/PowerPoint.
FONT_REGULAR_PATH = r"C:\Windows\Fonts\cambria.ttc"
FONT_BOLD_PATH    = r"C:\Windows\Fonts\cambriab.ttf"

BODY = 11.0
SUB = 7.5
SUPER = 7.5
H1 = 18.0
H2 = 14.0


def build():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4

    # Registrar fuentes Unicode
    page.insert_font(fontname="ArialU", fontfile=FONT_REGULAR_PATH)
    page.insert_font(fontname="ArialB", fontfile=FONT_BOLD_PATH)

    # Font objects para medir ancho de cualquier TTF (Arial built-in en fitz no soporta width)
    font_reg  = fitz.Font(fontfile=FONT_REGULAR_PATH)
    font_bold = fitz.Font(fontfile=FONT_BOLD_PATH)

    def width(text, size, font="ArialU"):
        f = font_bold if font == "ArialB" else font_reg
        return f.text_length(text, fontsize=size)

    def put(x, y, text, size=BODY, font="ArialU"):
        page.insert_text((x, y), text, fontsize=size, fontname=font)
        return width(text, size, font)

    y = 60
    # ── Título capítulo ───────────────────────────────────────────────────────
    put(50, y, "Chapter 3. Relativistic Mechanics", size=H1, font="ArialB")
    y += 35

    # ── Heading sección ───────────────────────────────────────────────────────
    put(50, y, "3.1 Mass-Energy Equivalence", size=H2, font="ArialB")
    y += 25

    # ── Párrafo con ligadura tipográfica ─────────────────────────────────────
    # ﬁ (U+FB01) es la ligadura tipográfica fi
    put(50, y, "The most famous equation in modern physics deﬁnes the equivalence")
    y += 16
    put(50, y, "between mass and energy. The relation was ﬁrst derived by Einstein")
    y += 16
    put(50, y, "in his 1905 paper and reads as follows:")
    y += 30

    # ── Ecuación display: E² = p²c² + (mc²)² ─────────────────────────────────
    # Cada glifo en su tamaño y baseline correctos
    cur = 230
    cur += put(cur, y, "E")
    page.insert_text((cur, y - 5), "2", fontsize=SUPER, fontname="ArialU")
    cur += width("2", SUPER, font="ArialU")
    cur += put(cur, y, " = p")
    page.insert_text((cur, y - 5), "2", fontsize=SUPER, fontname="ArialU")
    cur += width("2", SUPER, font="ArialU")
    cur += put(cur, y, "c")
    page.insert_text((cur, y - 5), "2", fontsize=SUPER, fontname="ArialU")
    cur += width("2", SUPER, font="ArialU")
    cur += put(cur, y, " + (mc")
    page.insert_text((cur, y - 5), "2", fontsize=SUPER, fontname="ArialU")
    cur += width("2", SUPER, font="ArialU")
    cur += put(cur, y, ")")
    page.insert_text((cur, y - 5), "2", fontsize=SUPER, fontname="ArialU")
    put(500, y, "(3.1)")
    y += 30

    # ── Texto con cross-line hyphenation ─────────────────────────────────────
    put(50, y, "where p is the relativistic momentum of the particle, c is the speed")
    y += 16
    put(50, y, "of light in vacuum, and m is the invariant rest mass. This conti-")
    y += 16
    put(50, y, "nuity equation must be preserved under any Lorentz transformation.")
    y += 28

    # ── Sección 3.2 ──────────────────────────────────────────────────────────
    put(50, y, "3.2 The Schrödinger Equation", size=H2, font="ArialB")
    y += 25

    put(50, y, "In quantum mechanics, the time-dependent Schrödinger equation is:")
    y += 28

    # ── Ecuación: iℏ ∂ψ/∂t = Ĥψ ─────────────────────────────────────────────
    put(230, y, "iℏ ∂ψ/∂t = Ĥψ")
    put(500, y, "(3.2)")
    y += 30

    # ── Integral con límites: ∫₀^∞ e^(-x) dx = 1 ──────────────────────────────
    put(50, y, "A canonical integral that appears throughout statistical mechanics:")
    y += 30

    cur = 240
    page.insert_text((cur, y), "∫", fontsize=BODY + 4, fontname="ArialU")
    page.insert_text((cur + 10, y - 8), "∞", fontsize=SUPER, fontname="ArialU")
    page.insert_text((cur + 10, y + 4), "0", fontsize=SUB, fontname="ArialU")
    cur += 22
    cur += put(cur, y, " e")
    page.insert_text((cur, y - 5), "−x", fontsize=SUPER, fontname="ArialU")
    cur += width("−x", SUPER, font="ArialU")
    put(cur, y, " dx = 1")
    put(500, y, "(3.3)")
    y += 30

    # ── Sumatoria: e^x = Σ x^n/n! ────────────────────────────────────────────
    put(50, y, "The exponential function admits the Taylor series expansion:")
    y += 30

    cur = 230
    cur += put(cur, y, "e")
    page.insert_text((cur, y - 5), "x", fontsize=SUPER, fontname="ArialU")
    cur += width("x", SUPER, font="ArialU")
    cur += put(cur, y, " = ")
    # Σ con límites
    page.insert_text((cur, y), "∑", fontsize=BODY + 4, fontname="ArialU")
    page.insert_text((cur + 4, y - 8), "∞", fontsize=SUPER, fontname="ArialU")
    page.insert_text((cur + 2, y + 5), "n=0", fontsize=SUB, fontname="ArialU")
    cur += 26
    cur += put(cur, y, "x")
    page.insert_text((cur, y - 5), "n", fontsize=SUPER, fontname="ArialU")
    cur += width("n", SUPER, font="ArialU")
    put(cur, y, "/n!")
    put(500, y, "(3.4)")
    y += 32

    # ── Sección con química ─────────────────────────────────────────────────
    put(50, y, "3.3 Notation in Chemistry", size=H2, font="ArialB")
    y += 25

    put(50, y, "Subscripts are equally important in chemical formulae. Water is")
    y += 16
    # H2O, H2SO4 con subíndices reales
    cur = 50
    cur += put(cur, y, "written as H")
    page.insert_text((cur, y + 4), "2", fontsize=SUB, fontname="ArialU")
    cur += width("2", SUB, font="ArialU")
    cur += put(cur, y, "O, sulfuric acid as H")
    page.insert_text((cur, y + 4), "2", fontsize=SUB, fontname="ArialU")
    cur += width("2", SUB, font="ArialU")
    cur += put(cur, y, "SO")
    page.insert_text((cur, y + 4), "4", fontsize=SUB, fontname="ArialU")
    cur += width("4", SUB, font="ArialU")
    cur += put(cur, y, ", and")

    y += 16
    # Nₐ = 6.022 × 10²³ mol⁻¹
    cur = 50
    cur += put(cur, y, "Avogadro's number is N")
    page.insert_text((cur, y + 4), "A", fontsize=SUB, fontname="ArialU")
    cur += width("A", SUB, font="ArialU")
    cur += put(cur, y, " = 6.022 × 10")
    page.insert_text((cur, y - 5), "23", fontsize=SUPER, fontname="ArialU")
    cur += width("23", SUPER, font="ArialU")
    cur += put(cur, y, " mol")
    page.insert_text((cur, y - 5), "−1", fontsize=SUPER, fontname="ArialU")
    cur += width("−1", SUPER, font="ArialU")
    put(cur, y, ".")
    y += 30

    # ── Greek letters paragraph ──────────────────────────────────────────────
    put(50, y, "3.4 Greek Symbols in Physics", size=H2, font="ArialB")
    y += 25
    put(50, y, "Common Greek letters: α (alpha), β (beta), γ (gamma), Δ (Delta),")
    y += 16
    put(50, y, "λ (lambda), μ (mu), π (pi), σ (sigma), φ (phi), ω (omega).")
    y += 16
    put(50, y, "Operators: ∇ (nabla), ∂ (partial), ∫ (integral), √ (radical).")

    doc.save(str(OUT_PDF))
    doc.close()

    # ── Ground truth ─────────────────────────────────────────────────────────
    gt = {
        "title": "Chapter 3. Relativistic Mechanics",
        "headings": [
            "3.1 Mass-Energy Equivalence",
            "3.2 The Schrödinger Equation",
            "3.3 Notation in Chemistry",
            "3.4 Greek Symbols in Physics",
        ],
        "equations_latex": [
            "E^{2} = p^{2}c^{2} + (mc^{2})^{2}",
            r"i\hbar \partial\psi/\partial t = \hat{H}\psi",
            r"\int_{0}^{\infty} e^{-x} dx = 1",
            r"e^{x} = \sum_{n=0}^{\infty} x^{n}/n!",
        ],
        "chemistry_formulae": ["H_{2}O", "H_{2}SO_{4}", "N_{A} = 6.022×10^{23} mol^{-1}"],
        "must_contain_words": [
            "defines",
            "first",
            "continuity",
            "Schrödinger",
            "Avogadro",
        ],
        "must_contain_symbols": [
            "α", "β", "γ", "Δ", "λ", "μ", "π", "σ", "φ", "ω",
            "∇", "∂", "∫", "√", "∑", "∞", "ℏ", "ψ",
        ],
        "must_not_contain": [
            "ﬁ", "ﬀ",
            "conti- nuity",
            "deﬁnes",
            "ﬁrst",
        ],
        "semantic_checks": [
            {"context": "E", "must_be_followed_by": "^", "in_equation": True},
            {"context": "p", "must_be_followed_by": "^", "in_equation": True},
            {"context": "H", "must_be_followed_by": "_", "in_equation": True},
            {"context": "10", "must_be_followed_by": "^", "in_equation": True},
        ],
    }
    OUT_GT.write_text(json.dumps(gt, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"PDF generado:    {OUT_PDF}")
    print(f"Ground truth:    {OUT_GT}")
    print(f"Páginas:         {len(fitz.open(str(OUT_PDF)))}")
    print(f"Tamaño:          {OUT_PDF.stat().st_size:,} bytes")


if __name__ == "__main__":
    build()
