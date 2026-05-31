"""
gen_scanned_pdf.py — Genera un PDF "escaneado" a partir de un .md del cache.

Idea: tomar texto real de la biblioteca, renderizarlo como imágenes (cada
página = bitmap), y empaquetarlo en un PDF. El resultado es indistinguible
de un PDF escaneado real para PyMuPDF — fuerza la ruta OCR del pipeline.

Salida: loadtest/sample_scanned.pdf

Uso:
    python loadtest/gen_scanned_pdf.py [--pages N] [--source <ruta-md>]
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont


def find_source_md(cache_dir: Path) -> Path:
    """Elige el primer .md no vacío de tamaño moderado (>50 KB, <2 MB)."""
    candidates = sorted(cache_dir.glob("*.md"))
    for p in candidates:
        size = p.stat().st_size
        if 50_000 < size < 2_000_000:
            return p
    if not candidates:
        raise FileNotFoundError(f"No hay .md en {cache_dir}")
    return candidates[0]


def _load_unicode_font(size: int) -> ImageFont.FreeTypeFont:
    """Carga una TTF del sistema con soporte unicode (latin-1 + es)."""
    candidates = [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
        r"C:\Windows\Fonts\seguisb.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _ascii_safe(s: str) -> str:
    """
    Limpia el markdown para OCR: quita formato MD, normaliza espacios y mantiene
    el texto humano. EasyOCR funciona mejor con texto plano y bien espaciado.
    """
    s = re.sub(r"!\[.*?\]\(.*?\)", "", s)       # imágenes
    s = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", s)   # enlaces → texto
    s = re.sub(r"[`*_#>]+", " ", s)              # markers MD
    s = re.sub(r"\s+", " ", s)                   # colapsa whitespace
    return s.strip()


def render_text_to_scanned_pdf(text: str, out_path: Path, n_pages: int) -> None:
    """
    Renderiza `text` como `n_pages` páginas-imagen via PIL + TTF unicode.

    Por qué PIL en vez de fitz.insert_textbox:
      PyMuPDF con fontname='helv' usa Helvetica base-14 (sin acentos/ñ);
      caracteres no soportados se descartan silenciosamente → página vacía.
      PIL con TTF (Arial/Calibri de Windows) garantiza renderizado completo.
    """
    text = _ascii_safe(text)
    words = text.split()
    chunk = max(1, len(words) // n_pages)
    chunks = [" ".join(words[i:i + chunk]) for i in range(0, len(words), chunk)][:n_pages]
    while len(chunks) < n_pages:
        chunks.append(chunks[-1] if chunks else "Texto de prueba.")

    # A4 @ 200 DPI = 1654x2339 px
    W, H = 1654, 2339
    MARGIN = 150
    FONT_PX = 36          # ~13pt @ 200 DPI, altura legible para EasyOCR
    LINE_HEIGHT = 48
    font = _load_unicode_font(FONT_PX)

    scanned = fitz.open()

    for content in chunks:
        img = Image.new("L", (W, H), color=255)  # fondo blanco
        draw = ImageDraw.Draw(img)

        # Wrap manual: corta líneas que excederían el ancho útil
        max_chars_per_line = (W - 2 * MARGIN) // (FONT_PX // 2)
        words_left = content.split()
        lines = []
        current = []
        for w in words_left:
            test = " ".join(current + [w])
            if len(test) > max_chars_per_line:
                if current:
                    lines.append(" ".join(current))
                current = [w]
            else:
                current.append(w)
        if current:
            lines.append(" ".join(current))

        max_lines = (H - 2 * MARGIN) // LINE_HEIGHT
        lines = lines[:max_lines]

        y = MARGIN
        for line in lines:
            draw.text((MARGIN, y), line, fill=0, font=font)
            y += LINE_HEIGHT

        # Convierte PIL image → bytes PNG → pixmap fitz → página PDF
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        pix = fitz.Pixmap(buf.getvalue())
        page = scanned.new_page(width=pix.width, height=pix.height)
        page.insert_image(page.rect, pixmap=pix)

    scanned.save(str(out_path), garbage=4, deflate=True)
    scanned.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=10,
                    help="Número de páginas del PDF generado (default 10).")
    ap.add_argument("--source", default=None,
                    help="Ruta a un .md específico (default: auto-pick del cache).")
    ap.add_argument("--out", default="loadtest/sample_scanned.pdf")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent

    if args.source:
        md_path = Path(args.source)
    else:
        cache_dir = root / "conversion_cache"
        md_path = find_source_md(cache_dir)

    print(f"[source] {md_path}  ({md_path.stat().st_size / 1024:.1f} KB)")
    text = md_path.read_text(encoding="utf-8", errors="ignore")[:120_000]

    out_path = root / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    render_text_to_scanned_pdf(text, out_path, args.pages)

    final_size = out_path.stat().st_size
    print(f"[out]    {out_path}  ({final_size / 1024:.1f} KB, {args.pages} pages)")


if __name__ == "__main__":
    sys.exit(main() or 0)
