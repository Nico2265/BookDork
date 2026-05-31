"""Renderiza el recorte impreso real de los glifos no resueltos top para verificarlos."""
from __future__ import annotations
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND.resolve()))
import fitz  # noqa: E402
import glyph_name_recovery as gnr  # noqa: E402

PDF = Path(r"C:\Users\nicof\Downloads\algebra-y-trigonometria-con-geometria-analitica-12ed.pdf")
OUT = ROOT / "tests" / "glyph_verify"
OUT.mkdir(exist_ok=True)

# Glifos top no resueltos (del análisis del MD en caché)
TARGETS = ["H20850","H9258","H20862","H20852","H20873","H20874","H20857",
           "H20876","H20875","H9252","H20877","H11546","H17006","H17007","H11004"]

def _is_broken(c: str) -> bool:
    if not c:
        return False
    o = ord(c)
    return o < 0x20 or 0x80 <= o <= 0x9F or 0xE000 <= o <= 0xF8FF or o in (0xFFFD, 0xFFFE, 0xFFFF)

doc = fitz.open(str(PDF))
PAGES = min(220, doc.page_count)
rec = gnr.GlyphNameRecovery(doc, supplement=gnr.load_supplement())

# name -> (font_name, code) y font_name -> xref
from bench_glyph_name_recovery import _font_name_to_xref, _strip_subset  # type: ignore
name2xref = _font_name_to_xref(doc, list(range(PAGES)))

# Buscar primera aparición (page, bbox) de cada target
found: dict[str, tuple] = {}
remaining = set(TARGETS)
for i in range(PAGES):
    if not remaining:
        break
    raw = doc[i].get_text("rawdict")
    for b in raw.get("blocks", []):
        for l in b.get("lines", []):
            for s in l.get("spans", []):
                font = s.get("font", "")
                xref = name2xref.get(_strip_subset(font))
                if xref is None:
                    continue
                diag = rec.font_diagnostics(xref)
                for ch in s.get("chars", []):
                    c = ch.get("c")
                    if not c or not _is_broken(c):
                        continue
                    name = diag.code_to_name.get(ord(c))
                    if name in remaining:
                        found[name] = (i, fitz.Rect(ch["bbox"]), font)
                        remaining.discard(name)

print("encontrados:", sorted(found), "| faltan:", sorted(remaining))

# Renderizar SOLO el bbox exacto (sin vecinos) → glifo inequívoco
for name, (pno, bbox, font) in found.items():
    pad = 0.4  # mínimo, solo para no cortar antialiasing
    clip = fitz.Rect(bbox.x0 - pad, bbox.y0 - pad, bbox.x1 + pad, bbox.y1 + pad)
    pix = doc[pno].get_pixmap(matrix=fitz.Matrix(16, 16), clip=clip)
    pix.save(str(OUT / f"{name}.png"))
print("PNGs en", OUT)
doc.close()
