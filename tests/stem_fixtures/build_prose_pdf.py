"""Genera un PDF de prosa pura (sin matemática) para test de no-regresión."""
from __future__ import annotations
from pathlib import Path
import fitz

OUT = Path(__file__).parent / "prose_sample.pdf"

PROSE = """The Old Man and the Sea

He was an old man who fished alone in a skiff in the Gulf Stream and he
had gone eighty-four days now without taking a fish. In the first forty
days a boy had been with him. But after forty days without a fish the
boy's parents had told him that the old man was now definitely and
finally salao, which is the worst form of unlucky, and the boy had gone
at their orders in another boat which caught three good fish the first
week.

It made the boy sad to see the old man come in each day with his skiff
empty and he always went down to help him carry either the coiled lines
or the gaff and harpoon and the sail that was furled around the mast.
The sail was patched with flour sacks and, furled, it looked like the
flag of permanent defeat.
"""


def build():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="Cambria", fontfile=r"C:\Windows\Fonts\cambria.ttc")
    y = 60
    for line in PROSE.split("\n"):
        page.insert_text((50, y), line, fontsize=11, fontname="Cambria")
        y += 16
        if y > 800:
            page = doc.new_page(width=595, height=842)
            page.insert_font(fontname="Cambria", fontfile=r"C:\Windows\Fonts\cambria.ttc")
            y = 60
    doc.save(str(OUT))
    print(f"PROSE PDF: {OUT}  ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    build()
