"""
=============================================================================
stem_engine.py — Pipeline de alta fidelidad para PDFs científicos / matemáticos
=============================================================================
Convierte PDFs digitales con notación STEM a Markdown preservando:

  * Super/subíndices (E², H₂O, x^n)              ← análisis baseline + tamaño
  * Símbolos matemáticos (∫, ∑, ∂, ∇, ℏ, α…)    ← Unicode pass-through con LaTeX opcional
  * Estructura (headings por tamaño de fuente)
  * Ligaduras tipográficas (ﬁ→fi, ﬀ→ff)         ← expansión NFKC + tabla específica
  * Mojibake y caracteres rotos (￾, �)           ← ftfy
  * Palabras partidas con guión de salto         ← reparación cross-line
  * Soft-hyphen invisible (U+00AD)               ← strip

NO procesa imágenes ni OCR — eso queda en pdf_engine.py para PDFs escaneados.

API pública:
    score_stem_density(doc) -> float       Densidad STEM en [0,1]
    convert_document(content, filename)    Devuelve dict compatible con pdf_engine

Diseño: módulo independiente. pdf_engine.py lo invoca opcionalmente vía
routing por densidad. Cualquier excepción en stem_engine cae al pipeline
original — cero riesgo de regresión.
=============================================================================
"""
from __future__ import annotations

import logging
import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import fitz   # PyMuPDF

logger = logging.getLogger("bookdork.stem_engine")

# ─────────────────────────────────────────────────────────────────────────────
# Constantes de detección STEM
# ─────────────────────────────────────────────────────────────────────────────

# Símbolos cuya presencia indica contenido matemático/científico
_MATH_SYMBOLS = frozenset(
    "∫∮∯∰∱∲∳"        # integrales
    "∑∏∐"             # sumatorias / productos
    "∂∇"              # operadores diferenciales
    "√∛∜"             # raíces
    "≠≤≥≈≡≢≜≝≅≃≄"    # relaciones
    "±∓×÷⋅∗⊕⊖⊗⊘⊙"    # operadores
    "∈∉∋∌⊂⊃⊆⊇∩∪"    # conjuntos
    "∀∃∄∅"            # cuantificadores
    "→←↔⇒⇐⇔↦"         # flechas / lógica
    "∞ℵℶℷ"            # infinito / ordinales
    "ℏℎℓ℘ℜℑℵ"         # planck / letras especiales
    "αβγδεζηθικλμνξοπρστυφχψω"           # greek minúsc
    "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"          # greek mayúsc
    "²³¹⁰⁴⁵⁶⁷⁸⁹"      # superíndices Unicode
    "₀₁₂₃₄₅₆₇₈₉"      # subíndices Unicode
)

# Familias de fuentes asociadas a notación matemática en PDFs LaTeX/Word
_MATH_FONT_PATTERNS = (
    re.compile(r"\b(CM[A-Z]+|cm[a-z]+)\b"),    # Computer Modern: CMR, CMSY, CMMI, CMEX, CMBX…
    re.compile(r"\b(MTSY|MTMI|MTEX|MTSYN)\b"), # MathTime
    re.compile(r"\bMath", re.I),                # MathJax, MathFont, "Math"
    re.compile(r"\b(STIX|stix)"),               # STIX
    re.compile(r"\bAMS\w*\b"),                  # AMS fonts
    re.compile(r"\bEuler\w*\b", re.I),
    re.compile(r"\bSymbol\b"),                  # Symbol font (Helvetica Symbol etc.)
    re.compile(r"\b(rsfs|euex|msam|msbm)\b"),
)


def _font_is_math(font_name: str) -> bool:
    if not font_name:
        return False
    return any(pat.search(font_name) for pat in _MATH_FONT_PATTERNS)


# ─────────────────────────────────────────────────────────────────────────────
# Normalización Unicode (corre sobre TODO el output)
# ─────────────────────────────────────────────────────────────────────────────

# Ligaduras tipográficas → ASCII equivalente (NFKC no las descompone todas)
_LIGATURES = {
    "ﬀ": "ff",   "ﬁ": "fi",   "ﬂ": "fl",
    "ﬃ": "ffi",  "ﬄ": "ffl",  "ﬅ": "ft",  "ﬆ": "st",
}

# Alias matemáticos: distintos codepoints que representan el mismo símbolo.
# Cambria Math y otras fuentes a veces mapean hbar (la constante de Planck/2π)
# a U+0127 (ħ, letra latina h con trazo, usada en maltés) cuando el codepoint
# semánticamente correcto es U+210F (ℏ). Normalizamos al canónico.
_MATH_ALIASES = {
    "ħ": "ℏ",     # U+0127 → U+210F  (Planck h-bar)
    "Ø": "∅",     # U+00D8 letra → U+2205 conjunto vacío (solo en contexto math; ver nota)
    # NOTA: Ø como letra (danesa/noruega) NO se debe convertir. Esta conversión
    # se aplica solo si la heurística detecta contexto matemático. Por seguridad,
    # la dejamos comentada por defecto:
    # (no la aplicamos automáticamente)
}
# Quitamos la entrada Ø para no romper texto noruego/danés
_MATH_ALIASES.pop("Ø", None)

# Comillas tipográficas y caracteres de espaciado raros → ASCII estándar
_QUOTES_AND_SPACES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "′": "'", "″": '"',
    " ": " ",   # NBSP → space
    " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", " ": " ", " ": " ",
    " ": " ", " ": " ", " ": " ",
    "​": "",    # ZWSP → strip
    "‌": "", "‍": "",  # ZWNJ, ZWJ → strip
    "﻿": "",    # BOM → strip
}

# Soft hyphen U+00AD: marca tipográfica de "puede romperse aquí". Algunos PDFs
# (sobre todo si la fuente tiene ToUnicode roto) lo emiten en lugar del guión
# ASCII U+002D. Si lo borrásemos a ciegas perderíamos guiones que SÍ deben verse
# (e.g., "Mass-Energy" extraído como "Mass<U+00AD>Energy"). Estrategia segura:
#   1. Si está al final de palabra y antes de salto de línea  → elimina (era hyphenation)
#   2. Si está entre dos letras sin salto de línea            → convierte a U+002D
# La paso 1 la maneja _repair_hyphenation (busca cualquier dash + \n).
_SOFT_HYPHEN = "­"

# Hyphen variations → ASCII hyphen-minus
_DASHES_TO_HYPHEN = {
    "‐": "-",   # HYPHEN
    "‑": "-",   # NON-BREAKING HYPHEN
    "‒": "-",   # FIGURE DASH
    "–": "-",   # EN DASH (cuando aparece como separador léxico)
}
# EM DASH y guiones similares en prosa se preservan como están (—)


def _build_translation_table() -> dict:
    table = {}
    for k, v in _LIGATURES.items():
        table[ord(k)] = v
    for k, v in _MATH_ALIASES.items():
        table[ord(k)] = v
    for k, v in _QUOTES_AND_SPACES.items():
        table[ord(k)] = v if v else None  # None = delete
    # Soft hyphen: convertir a ASCII hyphen — la limpieza de hyphenation
    # cross-line ya elimina los que correspondan a partición de palabra.
    table[ord(_SOFT_HYPHEN)] = "-"
    for k, v in _DASHES_TO_HYPHEN.items():
        table[ord(k)] = v

    # ── Caracteres que NUNCA deben aparecer en el Markdown final ──────────────
    # Para uso académico/IA: cero caracteres de control o "basura" que rompan
    # parsers o contaminen el contenido. Se eliminan (None = delete).
    #
    #   C0 control  U+0000–U+001F  excepto \t (09) \n (0A) \r (0D)
    #   DEL         U+007F
    #   C1 control  U+0080–U+009F  (incluye los control bytes \x80..\x9F que
    #               emiten fuentes Adobe Type1 con CMap roto, ej. MathematicalPi)
    #   U+FFFD      REPLACEMENT CHARACTER (mojibake irrecuperable)
    #   U+FFFE/FFFF noncharacters
    #   U+FFFC      OBJECT REPLACEMENT (imagen embebida sin texto)
    # NULL (U+0000) ya queda cubierto por el rango C0.
    for cp in range(0x00, 0x20):
        if cp not in (0x09, 0x0A, 0x0D):
            table[cp] = None
    table[0x7F] = None
    for cp in range(0x80, 0xA0):
        table[cp] = None
    table[0xFFFC] = None
    table[0xFFFD] = None
    table[0xFFFE] = None
    table[0xFFFF] = None
    return table


_TRANSLATE_TABLE = _build_translation_table()


# ─────────────────────────────────────────────────────────────────────────────
# Auditoría e integridad del juego de caracteres (garantía "sin desconocidos")
# ─────────────────────────────────────────────────────────────────────────────
#
# Para uso académico / investigación / IA el output NO puede contener glifos
# que ni un humano ni un parser puedan interpretar. `audit_charset` clasifica
# cualquier carácter sospechoso que sobreviva a la normalización; el pipeline
# lo usa como red de seguridad final (y para logging/QA).

# Rango Private Use Area: glifos de fuentes con CMap roto que font_recovery
# no logró mapear. No tienen semántica Unicode pública → sospechosos.
def _is_private_use(cp: int) -> bool:
    return (0xE000 <= cp <= 0xF8FF
            or 0xF0000 <= cp <= 0xFFFFD
            or 0x100000 <= cp <= 0x10FFFD)


def _is_suspect_char(ch: str) -> bool:
    """
    True si el carácter no debería aparecer en un Markdown académico limpio.
    Permite: ASCII imprimible, whitespace estándar, y cualquier carácter con
    categoría Unicode "real" (letras, números, puntuación, símbolos, marcas).
    Marca como sospechoso: control (Cc), formato (Cf) salvo los inofensivos,
    no-asignados (Cn), surrogate (Cs), y Private Use (Co).
    """
    if ch in ("\t", "\n", "\r", " "):
        return False
    cat = unicodedata.category(ch)
    # Cc=control, Cf=format, Cs=surrogate, Co=private-use, Cn=unassigned
    if cat in ("Cc", "Cs", "Co", "Cn"):
        return True
    if cat == "Cf":
        # Cf incluye marcas direccionales/joiners invisibles → fuera.
        return True
    return False


def audit_charset(text: str) -> dict:
    """
    QA: devuelve un informe de caracteres sospechosos presentes en `text`.

    Returns dict:
      clean: bool                  — True si no hay nada sospechoso
      total_suspect: int           — nº de ocurrencias sospechosas
      by_codepoint: dict[str,int]  — {"U+FFFD": n, ...} para diagnóstico
    """
    counts: dict[str, int] = {}
    total = 0
    for ch in text:
        if _is_suspect_char(ch):
            key = f"U+{ord(ch):04X}"
            counts[key] = counts.get(key, 0) + 1
            total += 1
    return {
        "clean": total == 0,
        "total_suspect": total,
        "by_codepoint": counts,
    }


def _sanitize_charset(text: str) -> str:
    """
    Red de seguridad final: elimina cualquier carácter sospechoso que haya
    sobrevivido a la tabla de traducción (p.ej. glifos Private-Use de fuentes
    con CMap roto que font_recovery no pudo mapear). Garantiza que el Markdown
    entregado no contenga caracteres que rompan parsers o confundan a una IA.
    """
    if not text:
        return text
    if all(not _is_suspect_char(c) for c in text):
        return text
    return "".join(c for c in text if not _is_suspect_char(c))


# Reparación de palabras partidas: "conti-\nnuity" → "continuity"
# Acepta tanto guión ASCII U+002D como (después de translate) cualquier dash
# normalizado. Heurística:
#   1. Antes del guión hay letra (ASCII o Latín extendido)
#   2. Después del salto de línea hay letra minúscula (los nombres propios
#      con mayúscula raramente son partición de palabra)
_HYPHEN_BREAK = re.compile(r"([A-Za-zÀ-ÖØ-öø-ÿ])-[ \t]*\n[ \t]*([a-zà-öø-ÿ])")


def _repair_hyphenation(text: str) -> str:
    return _HYPHEN_BREAK.sub(r"\1\2", text)


# ─────────────────────────────────────────────────────────────────────────────
# Superíndices / subíndices Unicode → marcadores LaTeX  (CRÍTICO para exponentes)
# ─────────────────────────────────────────────────────────────────────────────
# Cuando un PDF emite el exponente como un ÚNICO codepoint Unicode (x², H₂O, 10⁶)
# en vez de un glifo pequeño elevado, NFKC lo APLASTA a dígito normal (x²→x2),
# destruyendo la semántica del exponente. Para evitarlo, convertimos estos
# caracteres a marcadores `^{}` / `_{}` ANTES de NFKC. La detección geométrica
# (glifos elevados como líneas/spans separados) NO pasa por aquí — ya emitió `^`.

_SUPERSCRIPT_BASE = {
    0x00B2: "2", 0x00B3: "3", 0x00B9: "1",
    0x2070: "0", 0x2071: "i", 0x2074: "4", 0x2075: "5", 0x2076: "6",
    0x2077: "7", 0x2078: "8", 0x2079: "9",
    0x207A: "+", 0x207B: "-", 0x207C: "=", 0x207D: "(", 0x207E: ")", 0x207F: "n",
}
_SUBSCRIPT_BASE = {
    0x2080: "0", 0x2081: "1", 0x2082: "2", 0x2083: "3", 0x2084: "4",
    0x2085: "5", 0x2086: "6", 0x2087: "7", 0x2088: "8", 0x2089: "9",
    0x208A: "+", 0x208B: "-", 0x208C: "=", 0x208D: "(", 0x208E: ")",
    0x2090: "a", 0x2091: "e", 0x2092: "o", 0x2093: "x", 0x2095: "h",
    0x2096: "k", 0x2097: "l", 0x2098: "m", 0x2099: "n", 0x209A: "p",
    0x209B: "s", 0x209C: "t",
    0x1D62: "i", 0x1D63: "r", 0x1D64: "u", 0x1D65: "v",
}


def _fmt_script(marker: str, content: str) -> str:
    """Forma compacta `^2` para 1 char alfanumérico; `^{...}` para el resto.
    Idéntico criterio que _emit_marker para mantener consistencia de salida."""
    if not content:
        return ""
    if len(content) == 1 and content.isascii() and (content.isalnum() or content == "-"):
        return f"{marker}{content}"
    return f"{marker}{{{content}}}"


def _convert_unicode_scripts(text: str) -> str:
    """
    Reemplaza runs de superíndices/subíndices Unicode por marcadores LaTeX:
    `x²³`→`x^{23}`, `H₂O`→`H_2O`, `x⁻¹`→`x^{-1}`. Debe correr ANTES de NFKC.
    """
    if not text:
        return text
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        o = ord(text[i])
        if o in _SUPERSCRIPT_BASE:
            buf = []
            while i < n and ord(text[i]) in _SUPERSCRIPT_BASE:
                buf.append(_SUPERSCRIPT_BASE[ord(text[i])]); i += 1
            out.append(_fmt_script("^", "".join(buf)))
        elif o in _SUBSCRIPT_BASE:
            buf = []
            while i < n and ord(text[i]) in _SUBSCRIPT_BASE:
                buf.append(_SUBSCRIPT_BASE[ord(text[i])]); i += 1
            out.append(_fmt_script("_", "".join(buf)))
        else:
            out.append(text[i]); i += 1
    return "".join(out)


def normalize_unicode(text: str) -> str:
    """
    Pipeline de normalización Unicode aplicable a CUALQUIER markdown extraído.

    Orden:
      1. ftfy.fix_text — repara mojibake (cp1252→utf8 mal decodificado, ￾, �)
      2. Tabla de traducción — ligaduras, soft-hyphen, quotes, dashes
      2b. Super/subíndices Unicode → `^{}`/`_{}`  (ANTES de NFKC, que los aplastaría)
      3. NFKC — descompone compatibilidad (e.g., ﬃ→ffi si quedó alguno)
      4. Reparación de palabras cross-line con guión
      5. Colapso de espacios múltiples y limpieza de fin de línea
    """
    if not text:
        return text

    # 1. ftfy
    try:
        from ftfy import fix_text
        text = fix_text(text, normalization="NFC")
    except ImportError:
        logger.debug("ftfy no disponible — saltando paso de mojibake repair")

    # 2. Tabla de traducción
    text = text.translate(_TRANSLATE_TABLE)

    # 2b. Super/subíndices Unicode → marcadores, ANTES de que NFKC los aplaste.
    text = _convert_unicode_scripts(text)

    # 3. NFKC para residuos
    text = unicodedata.normalize("NFKC", text)

    # 4. Re-translate por si NFKC produjo formas compuestas que aún están
    #    en nuestra tabla
    text = text.translate(_TRANSLATE_TABLE)

    # 5. Reparación de palabras partidas con guión
    text = _repair_hyphenation(text)

    # 6. Red de seguridad: elimina cualquier carácter sospechoso residual
    #    (Private-Use de fuentes rotas, control, no-asignados). Garantía de
    #    "cero caracteres desconocidos" para uso académico/IA.
    text = _sanitize_charset(text)

    # 7. Limpieza de whitespace: colapsa runs de espacios horizontales
    text = re.sub(r"[ \t]+", " ", text)
    # Limpia espacios al final de línea
    text = re.sub(r" +\n", "\n", text)
    # Colapsa 3+ saltos de línea consecutivos a 2 (separador de párrafo)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Detección de densidad STEM (routing)
# ─────────────────────────────────────────────────────────────────────────────

def score_stem_density(doc: "fitz.Document", sample_pages: int = 12) -> float:
    """
    Estima la densidad STEM del documento en [0, 1].

    Heurística:
      - Cuenta símbolos matemáticos / griegos en muestra de páginas
      - Cuenta glifos con tamaño anómalo (sub/superíndices candidatos)
      - Detecta fuentes matemáticas en spans
      - Combina con peso

    Sampling: se eligen las páginas UNIFORMEMENTE distribuidas a lo largo
    del documento (NO solo las primeras N). Esto evita falsos negativos
    en libros donde las primeras páginas son índice/prólogo sin matemática
    (textbooks típicos: las primeras 15-30 páginas son front matter).

    Threshold sugerido para routing: 0.05 (5% de signal STEM ya justifica
    pipeline pesado; los falsos positivos son baratos porque stem_engine
    funciona igual de bien en prosa pura).
    """
    n_total = len(doc)
    if n_total == 0:
        return 0.0
    n = min(sample_pages, n_total)

    # Sampling uniforme: para 12 muestras en libro de 724 páginas, elegimos
    # páginas en posiciones [60, 120, ..., 720]. Para libros pequeños cae
    # naturalmente a "todas las páginas".
    if n_total <= n:
        page_indices = list(range(n_total))
    else:
        step = n_total / n
        page_indices = [int(step * i + step / 2) for i in range(n)]

    total_chars = 0
    math_chars = 0
    sub_super_candidates = 0
    math_font_chars = 0

    for i in page_indices:
        page = doc[i]
        d = page.get_text("dict")
        # Densidad por página: clampamos cada página al [0,1] para que UNA
        # página muy densa (ej. solo ecuaciones) no domine sobre 20 páginas
        # de prosa. La densidad final es el MAX por página, no el promedio.
        # Esto evita falsos negativos donde mucha prosa diluye math denso.
        page_chars = 0
        page_math = 0
        page_subsup = 0
        page_mathfont = 0
        for block in d.get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                sizes = [s["size"] for s in spans]
                line_size = max(sizes) if sizes else 11.0
                for span in spans:
                    txt = span["text"]
                    sz = span["size"]
                    fnt = span.get("font", "")
                    chars = len(txt)
                    page_chars += chars
                    page_math += sum(1 for c in txt if c in _MATH_SYMBOLS)
                    if sz < line_size * 0.85 and txt.strip():
                        page_subsup += chars
                    if _font_is_math(fnt):
                        page_mathfont += chars
        total_chars += page_chars
        math_chars += page_math
        sub_super_candidates += page_subsup
        math_font_chars += page_mathfont

    if total_chars == 0:
        return 0.0

    math_ratio = math_chars / total_chars
    subsup_ratio = sub_super_candidates / total_chars
    mathfont_ratio = math_font_chars / total_chars

    # Peso: símbolos cuentan más que candidatos de sub/super (los headings
    # de tamaño pequeño podrían inflar subsup_ratio falsamente).
    score = math_ratio * 2.0 + subsup_ratio * 0.8 + mathfont_ratio * 3.0
    return min(1.0, score)


# ─────────────────────────────────────────────────────────────────────────────
# Extracción a nivel glifo con reconstrucción semántica
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Glyph:
    """Un glifo individual con metadatos de tipografía."""
    char: str
    size: float
    origin_y: float
    origin_x: float
    bbox_top: float
    font: str
    bbox_x0: float = 0.0   # borde izquierdo real del glifo (rawdict bbox[0])
    bbox_x1: float = 0.0   # borde derecho real del glifo  (rawdict bbox[2])

    @property
    def is_whitespace(self) -> bool:
        return self.char.isspace()


def _is_raw_broken_glyph(c: str) -> bool:
    """
    ¿El carácter emitido es un código de glifo CRUDO (fuente con CMap rota)?
    Solo en estos casos ord(c) == código de glifo y el canal de nombres aplica.
    Para chars imprimibles que MuPDF ya resolvió bien NO se toca (evita remapear
    p.ej. un dígito correcto cuyo code colisione con una entrada de /Differences).
    Cubre control C0 (sin whitespace), DEL, control C1 y Private-Use Area.
    """
    if not c:
        return False
    o = ord(c)
    if o < 0x20:
        return not c.isspace()
    if o == 0x7F or 0x80 <= o <= 0x9F:
        return True
    return (0xE000 <= o <= 0xF8FF or 0xF0000 <= o <= 0xFFFFD or 0x100000 <= o <= 0x10FFFD)


# Lazy cache del predicado de fuente rota (vive en font_recovery).
_font_is_broken_fn = None


def _is_broken_font(font: str) -> bool:
    """¿`font` es una familia con CMap rota conocida? (delega en font_recovery)."""
    global _font_is_broken_fn
    if _font_is_broken_fn is None:
        try:
            from . import font_recovery as _fr
        except ImportError:
            try:
                import font_recovery as _fr  # type: ignore
            except ImportError:
                _font_is_broken_fn = lambda _n: False  # noqa: E731
                return False
        _font_is_broken_fn = _fr._font_is_broken
    return bool(_font_is_broken_fn(font))


def _unresolved_marker(c: str, name: Optional[str]) -> str:
    """
    Marcador VISIBLE y diagnóstico para un glifo de fuente rota que NINGÚN canal
    pudo recuperar. Reemplaza el borrado SILENCIOSO (que hacía desaparecer
    símbolos sin dejar rastro — un 'error que engaña'). Principio: fallar fuerte.

    Lleva el nombre de glifo si se conoce (⟦?H17010⟧), o el codepoint crudo
    (⟦?U+E005⟧). Así un humano o una IA ve EXACTAMENTE qué se perdió y dónde, y
    el nombre queda cosechable para el suplemento. Los caracteres del marcador
    (⟦ ? ⟧) NO son sospechosos → sobreviven a _sanitize_charset.
    """
    tag = name if name else f"U+{ord(c):04X}"
    return f"⟦?{tag}⟧"


def _extract_glyphs(page, font_recovery=None, glyph_name_recovery=None,
                    glyph_table_recovery=None) -> list[list[_Glyph]]:
    """
    Devuelve lista de líneas, cada una con sus glifos en orden de lectura.

    Usa rawdict para tener char-level info: cada carácter trae su origin
    (baseline) y bbox separados, lo cual es lo que necesitamos para detectar
    super/subíndices con precisión.

    Las líneas devueltas por PyMuPDF a veces separan en `lines` distintas
    los super/subíndices que están en la misma fila visual (porque su
    bbox.top difiere de la del cuerpo). Aquí FUSIONAMOS líneas que comparten
    el cuerpo de la misma fila: si la origin_y del cuerpo (glifos grandes) de
    dos líneas consecutivas está dentro de ±line_height*0.6, son la misma fila.

    RECUPERACIÓN DE GLIFOS ROTOS (fuentes con CMap rota tipo MathematicalPi):
    se aplican DOS canales en orden de fiabilidad:
      1. `glyph_name_recovery` (DETERMINISTA): lee el nombre de glifo del
         /Differences del PDF y lo resuelve a Unicode vía AGL+suplemento. Sin
         ambigüedad — resuelve `=`/`+`/`−`/`Ω`… correctamente donde el matcher
         visual confundía `=`→`⇋`. Solo se invoca sobre glifos ROTOS (código
         crudo), donde ord(c) == código de glifo.
      2. `font_recovery` (VISUAL, fallback): matching de siluetas para lo que
         el canal de nombres no pudo resolver (se abstuvo). Probabilístico.
    Si el canal de nombres ya resolvió, el char deja de ser código de control
    y el matcher visual no se dispara (cortocircuito natural).
    """
    raw = page.get_text("rawdict")

    # Mapa per-página basename(sin subset)→xref para el canal de nombres.
    # rawdict expone el nombre de fuente del span, no su xref; lo resolvemos
    # contra get_fonts. (first-wins ante múltiples subsets: validado sobre Zill
    # con 0.1% de glifos sin nombre — la ambigüedad es despreciable en práctica.)
    name2xref: dict[str, int] = {}
    if glyph_name_recovery is not None:
        try:
            for f in page.get_fonts(full=True):
                name2xref.setdefault((f[3] or "").split("+")[-1], f[0])
        except Exception as exc:
            logger.debug("glyph_name_recovery: no se pudo mapear fuentes de página (%s)", exc)

    proto_lines: list[list[_Glyph]] = []
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            glyphs: list[_Glyph] = []
            for span in line.get("spans", []):
                size = span["size"]
                font = span.get("font", "")
                for ch in span.get("chars", []):
                    c = ch["c"]
                    if c is None:
                        continue
                    xref = name2xref.get(font.split("+")[-1]) if name2xref else None
                    # 0. Canal de TABLA (determinista, familia+codepoint) — la vía
                    #    más fiable. Captura el caso que los otros dos NO ven: glifo
                    #    de fuente-símbolo rota emitido como ASCII imprimible
                    #    incorrecto (`=`→`m`, `/`→`Y`). Solo entradas verificadas;
                    #    si no está tabulado, abstiene y deja seguir a los demás.
                    #    Gate por `covers()`: la tabla declara qué familias maneja,
                    #    así que cubre también fuentes-símbolo que el regex de
                    #    nombres NO reconoce (blind-spots: MT-Extra, ProgressivePi…)
                    #    sin tener que ampliar `_BROKEN_FONT_PATTERNS`.
                    if (glyph_table_recovery is not None and len(c) == 1
                            and glyph_table_recovery.covers(font)):
                        try:
                            mapped = glyph_table_recovery.resolve(font, ord(c))
                            if mapped is not None:
                                c = mapped
                        except Exception as exc:
                            logger.debug("glyph_table_recovery.resolve error (%s) — keep raw", exc)
                    # 1. Canal de nombres (determinista) — solo sobre glifos rotos
                    if glyph_name_recovery is not None and xref is not None and _is_raw_broken_glyph(c):
                        try:
                            res = glyph_name_recovery.resolve(xref, ord(c))
                            if res is not None:
                                c = res.char
                        except Exception as exc:
                            logger.debug("glyph_name_recovery.resolve error (%s) — keep raw", exc)
                    # 2. Recuperación visual (fallback) para fuentes rotas
                    if font_recovery is not None:
                        try:
                            if font_recovery.needs_recovery(font, c):
                                c = font_recovery.recover(page, span, c)
                        except Exception as exc:
                            logger.debug("font_recovery.recover error (%s) — keep raw", exc)
                    # 3. Ningún canal recuperó un glifo de FUENTE ROTA → marcador
                    #    visible y diagnóstico en vez de dejarlo crudo (que luego
                    #    _sanitize_charset borraría SILENCIOSAMENTE). Fallar fuerte.
                    if _is_raw_broken_glyph(c) and _is_broken_font(font):
                        name = None
                        if glyph_name_recovery is not None and xref is not None:
                            try:
                                name = glyph_name_recovery.name_for(xref, ord(c))
                            except Exception:
                                name = None
                        c = _unresolved_marker(c, name)
                    bbox = ch["bbox"]
                    glyphs.append(_Glyph(
                        char=c,
                        size=size,
                        origin_y=ch["origin"][1],
                        origin_x=ch["origin"][0],
                        bbox_top=bbox[1],
                        font=font,
                        bbox_x0=bbox[0],
                        bbox_x1=bbox[2],
                    ))
            if glyphs:
                proto_lines.append(glyphs)

    if not proto_lines:
        return []

    # Fusión de líneas vecinas que comparten fila visual ──────────────────────
    # Una línea de SÓLO super/subíndices (pocos glifos, tamaño pequeño) se
    # fusiona con su vecina si la origin_y está cerca.
    out: list[list[_Glyph]] = []

    def line_body_origin(gs: list[_Glyph]) -> tuple[float, float]:
        """Devuelve (origin_y del cuerpo, tamaño del cuerpo)."""
        non_ws = [g for g in gs if not g.is_whitespace]
        if not non_ws:
            # Línea de solo whitespace: usa el primer glifo como referencia
            return (gs[0].origin_y, gs[0].size)
        sz = max(g.size for g in non_ws)
        big = [g for g in non_ws if g.size >= sz * 0.95]
        if big:
            return (sum(g.origin_y for g in big) / len(big), sz)
        return (non_ws[0].origin_y, sz)

    for glyphs in proto_lines:
        if not out:
            out.append(glyphs)
            continue
        prev = out[-1]
        prev_y, prev_size = line_body_origin(prev)
        cur_y, cur_size = line_body_origin(glyphs)
        # Si la diferencia vertical es pequeña relativa al tamaño de cuerpo
        # mayor de las dos, es la misma fila visual (super/sub flotando)
        line_height = max(prev_size, cur_size)
        if abs(cur_y - prev_y) <= line_height * 0.6:
            # Fusionar en orden de X (preservar lectura izquierda→derecha)
            merged = sorted(prev + glyphs, key=lambda g: (round(g.origin_x, 1)))
            out[-1] = merged
        else:
            out.append(glyphs)

    return out


# Umbrales calibrados a tipografía estándar (Arial/Times/CM en cuerpo 10-14pt)
_BASELINE_SHIFT_MIN = 1.5    # px (~0.5em a 11pt) para detectar shift
_SIZE_RATIO_THRESHOLD = 0.85 # un glifo a ≤85% del tamaño de línea es candidato

# Recuperación de espacios: muchos PDFs no emiten glifos de espacio; el espacio
# es posicional. Si el hueco horizontal entre el borde derecho de un glifo y el
# izquierdo del siguiente supera esta fracción del tamaño de fuente, hubo un
# espacio. Calibrado conservador: un espacio tipográfico ≈ 0.25em; el kerning
# intra-palabra rara vez supera 0.12em. 0.18 separa palabras sin partir letras.
_WORD_SPACE_RATIO = 0.18

# Solape horizontal mínimo (fracción del menor ancho) entre el grupo superior e
# inferior de un cluster para considerarlos apilados (candidato a fracción).
_FRACTION_X_OVERLAP = 0.5

# De-fusión de columnas: un hueco horizontal que supere esta fracción del tamaño
# de fuente indica un gutter entre columnas FÍSICAS de la página (no un espacio
# entre palabras). Sin esto, dos columnas en la misma fila visual se concatenan
# y producen ECUACIONES FALSAS (ej. col-izq "= 3" + col-der "4x1" → "=34x1").
# Calibrado conservador: un espacio entre palabras ≈ 0.25em, separación entre
# ecuación y su número (3.X) ≈ 2-3em; un gutter de dos columnas ≥ 4em.
_COLUMN_GAP_RATIO = 4.0


def _classify_glyph_role(g: _Glyph, line_origin_y: float, line_size: float) -> str:
    """
    Clasifica el rol tipográfico de un glifo:
      'super'   superíndice
      'sub'     subíndice
      'body'    cuerpo de línea normal
    """
    if g.is_whitespace:
        return "body"
    size_ratio = g.size / line_size if line_size > 0 else 1.0
    if size_ratio >= _SIZE_RATIO_THRESHOLD:
        return "body"
    # Glifo más pequeño que la línea → puede ser super, sub o nota
    dy = g.origin_y - line_origin_y
    # En PDF las coords Y crecen HACIA ABAJO (en pymupdf rawdict).
    # Origin del baseline. Si origin_y del glifo es MENOR (más arriba en pantalla)
    # → superíndice. Si es MAYOR (más abajo) → subíndice.
    if dy < -_BASELINE_SHIFT_MIN:
        return "super"
    if dy > _BASELINE_SHIFT_MIN:
        return "sub"
    return "body"  # mismo baseline pero más pequeño = caso ambiguo, mejor no marcar


def _emit_marker(role: str, content: str) -> str:
    """Emite `^N` o `^{...}` / `_N` o `_{...}` según contenido."""
    marker = "^" if role == "super" else "_"
    stripped = content.strip()
    if not stripped:
        return ""
    if (len(stripped) == 1
            and stripped.isascii()
            and (stripped.isalnum() or stripped == "-")):
        return f"{marker}{stripped}"
    return f"{marker}{{{stripped}}}"


def _x_overlap_ratio(group_a: list[_Glyph], group_b: list[_Glyph]) -> float:
    """
    Solape horizontal entre dos grupos de glifos, como fracción del menor ancho.
    1.0 = perfectamente apilados (misma columna X); 0.0 = sin solape (en serie).
    """
    if not group_a or not group_b:
        return 0.0
    a0 = min(g.bbox_x0 for g in group_a); a1 = max(g.bbox_x1 for g in group_a)
    b0 = min(g.bbox_x0 for g in group_b); b1 = max(g.bbox_x1 for g in group_b)
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    narrower = min(a1 - a0, b1 - b0)
    if narrower <= 0:
        return 0.0
    return inter / narrower


def _wrap_runs(glyphs: list[_Glyph], body_size: float | None = None) -> str:
    """
    Reconstrucción semántica de la línea. Algoritmo:

      1. Clasifica cada glifo: body / super / sub
      2. Detecta CLUSTERS matemáticos: secuencia contigua de glifos no-body.
      3. Por cada cluster decide entre dos interpretaciones:
         a) FRACCIÓN: el grupo superior e inferior se apilan en la misma
            columna X (solape ≥ _FRACTION_X_OVERLAP) Y el cluster NO cuelga de
            una base alfanumérica a su izquierda (la guarda que evita confundir
            un índice tipo `x_i^2` con una fracción). → `\\frac{num}{den}`
         b) SUPER/SUBÍNDICE: caso normal → `_{subs}^{supers}` (orden LaTeX).
      4. Recupera ESPACIOS posicionales: si el hueco real entre el bbox del
         glifo previo y el siguiente supera _WORD_SPACE_RATIO·line_size, inserta
         un espacio (muchos PDFs no emiten glifos de espacio).

    Para 1 char ASCII alfanumérico emite forma compacta `^N` o `_N`.
    Para casos compuestos emite `^{...}` o `_{...}`.
    """
    if not glyphs:
        return ""

    # Tamaño y baseline del cuerpo de la línea
    # Tamaño de cuerpo y baseline ROBUSTOS. ANTES se usaba max(size): una
    # fracción/operador display (∫, ∑, barra de fracción) inflaba el máximo y
    # descuadraba el baseline → el texto normal parecía "elevado" → cascada de
    # falsos superíndices (^{=}, ^{sin}, ^{where}). Ahora el tamaño de cuerpo es
    # la MODA de tamaños (el más frecuente = el texto de cuerpo, mayoritario) y
    # el baseline es la MEDIANA de origin_y de los glifos de ese tamaño. Robusto
    # a unos pocos glifos gigantes.
    non_ws = [g for g in glyphs if not g.is_whitespace]
    if non_ws:
        size_counts = Counter(round(g.size * 2) / 2 for g in non_ws)  # bins de 0.5pt
        line_size = size_counts.most_common(1)[0][0] or max(g.size for g in non_ws)
        # ANCLA al tamaño de cuerpo del DOCUMENTO. La moda por-línea falla en
        # líneas matemáticas ESCASAS: en "= 4/3" hay 1 glifo de cuerpo ('=') y 2
        # pequeños (numerador/denominador), así que la moda elige el pequeño → el
        # baseline se hunde → los super/subíndices se aplanan a cuerpo ("= 43").
        #
        # El ancla SOLO SUBE line_size, nunca lo baja, y solo cuando la línea
        # contiene un glifo a la altura del cuerpo del documento. Tres guardas:
        #   1. body_size > line_size  → la moda local SUBESTIMÓ (dominaron glifos
        #      pequeños). En un heading (moda 14 > body 10) NO aplica: bajar a 10
        #      encogería el space_gap y rompería el espaciado de títulos.
        #   2. any(size >= 0.9·body)  → existe texto real de cuerpo en la línea.
        #      Una nota al pie entera en 8pt (sin glifo ≥9) NO se ancla, o se
        #      marcaría completa como superíndice.
        # Solo cuando ambas se cumplen elevamos el baseline al cuerpo real.
        if (body_size and body_size > line_size
                and any(g.size >= 0.9 * body_size for g in non_ws)):
            line_size = body_size
        body = [g for g in non_ws if 0.9 * line_size <= g.size <= 1.15 * line_size]
        ref = body if body else non_ws
        line_origin_y = statistics.median([g.origin_y for g in ref])
    else:
        line_size = max(g.size for g in glyphs)
        line_origin_y = glyphs[0].origin_y

    # Umbral de espacio basado en el tamaño de fuente de la línea.
    space_gap = line_size * _WORD_SPACE_RATIO

    roles = [_classify_glyph_role(g, line_origin_y, line_size) for g in glyphs]

    out: list[str] = []
    i = 0
    prev_x1 = None  # borde derecho real (bbox_x1) del último glifo no-ws emitido

    def maybe_insert_space(g: _Glyph) -> None:
        """Inserta un espacio si el hueco real respecto al glifo previo lo amerita."""
        nonlocal prev_x1
        if prev_x1 is not None and (g.bbox_x0 - prev_x1) > space_gap:
            if out and not out[-1].endswith((" ", "\n")):
                out.append(" ")

    def advance(g: _Glyph) -> None:
        nonlocal prev_x1
        if not g.is_whitespace:
            prev_x1 = g.bbox_x1

    while i < len(glyphs):
        role = roles[i]
        if role == "body":
            while i < len(glyphs) and roles[i] == "body":
                g = glyphs[i]
                if g.is_whitespace:
                    # Espacio explícito en el PDF: respétalo (sin duplicar)
                    if out and not out[-1].endswith((" ", "\n")):
                        out.append(" ")
                    i += 1
                    continue
                maybe_insert_space(g)
                out.append(g.char)
                advance(g)
                i += 1
        else:
            # ¿El cluster cuelga de una base alfanumérica inmediatamente a la
            # izquierda? Si sí, es un índice/exponente (no una fracción).
            prev_body = glyphs[i - 1] if i > 0 else None
            has_alnum_base = (
                prev_body is not None
                and not prev_body.is_whitespace
                and prev_body.char.isalnum()
                and roles[i - 1] == "body"
            )

            first = glyphs[i]
            maybe_insert_space(first)

            sub_glyphs: list[_Glyph] = []
            super_glyphs: list[_Glyph] = []
            while i < len(glyphs) and roles[i] != "body":
                cur = glyphs[i]
                if roles[i] == "super":
                    super_glyphs.append(cur)
                else:
                    sub_glyphs.append(cur)
                advance(cur)
                i += 1

            sub_text = "".join(g.char for g in sub_glyphs).strip()
            super_text = "".join(g.char for g in super_glyphs).strip()

            is_fraction = (
                bool(sub_text) and bool(super_text)
                and not has_alnum_base
                and _x_overlap_ratio(super_glyphs, sub_glyphs) >= _FRACTION_X_OVERLAP
            )

            if is_fraction:
                # Numerador arriba (super), denominador abajo (sub)
                out.append(f"\\frac{{{super_text}}}{{{sub_text}}}")
            else:
                # Orden LaTeX: subscript primero, luego superscript
                if sub_text:
                    out.append(_emit_marker("sub", sub_text))
                if super_text:
                    out.append(_emit_marker("super", super_text))

    return "".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# De-fusión de columnas físicas de página
# ─────────────────────────────────────────────────────────────────────────────

def _split_columns(glyphs: list[_Glyph]) -> list[list[_Glyph]]:
    """
    Parte una línea de glifos en segmentos cuando detecta un gutter entre
    columnas físicas de la página. Devuelve ≥1 segmentos en orden de lectura.

    Evita el modo de fallo más grave (ecuaciones falsas por concatenación de
    columnas). Conservador: solo parte en huecos ≥ _COLUMN_GAP_RATIO·line_size
    y solo si ambos lados tienen contenido no-trivial.
    """
    non_ws = [g for g in glyphs if not g.is_whitespace]
    if len(non_ws) < 2:
        return [glyphs]

    line_size = max(g.size for g in non_ws)
    threshold = line_size * _COLUMN_GAP_RATIO

    # Ordena por X para medir huecos reales en orden espacial.
    ordered = sorted(glyphs, key=lambda g: g.bbox_x0)
    segments: list[list[_Glyph]] = []
    current: list[_Glyph] = []
    prev_x1: Optional[float] = None
    for g in ordered:
        if g.is_whitespace:
            # El espacio no rompe; pero tampoco actualiza el borde derecho.
            if current:
                current.append(g)
            continue
        if prev_x1 is not None and (g.bbox_x0 - prev_x1) > threshold and current:
            segments.append(current)
            current = []
        current.append(g)
        prev_x1 = g.bbox_x1
    if current:
        segments.append(current)

    # Descarta segmentos vacíos o de solo whitespace y exige ≥1 char no-ws.
    segments = [s for s in segments if any(not g.is_whitespace for g in s)]
    return segments or [glyphs]


# ─────────────────────────────────────────────────────────────────────────────
# Headings por tamaño de fuente
# ─────────────────────────────────────────────────────────────────────────────

def _estimate_body_size(doc: "fitz.Document", sample_pages: int = 3) -> float:
    """
    Estima el tamaño de fuente del cuerpo de texto (la moda de tamaños frecuentes).
    Útil para clasificar headings (size > body * 1.15) sin sesgarse por sub/super.
    """
    sizes: dict[float, int] = {}
    n = min(sample_pages, len(doc))
    for i in range(n):
        page = doc[i]
        d = page.get_text("dict")
        for block in d.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    s = round(span["size"], 1)
                    chars = len(span["text"])
                    sizes[s] = sizes.get(s, 0) + chars
    if not sizes:
        return 11.0
    # Moda ponderada por número de caracteres
    return max(sizes.items(), key=lambda kv: kv[1])[0]


def _line_max_size(glyphs: list[_Glyph]) -> float:
    return max((g.size for g in glyphs if not g.is_whitespace), default=0.0)


_HEADING_MIN_LETTERS = 4  # un heading "real" requiere prosa, no solo símbolos


def _looks_like_heading(text: str) -> bool:
    """
    Acepta como heading solo líneas con prosa real, no símbolos sueltos
    ni ecuaciones display.

    Reglas:
      1. ≥4 letras ASCII (prosa real, no símbolos sueltos)
      2. NO contiene operadores matemáticos large (∫ ∑ ∏ ∮ etc.)
         — esos están en tamaño grande por ser operadores display
      3. NO contiene marcadores LaTeX `^{` o `_{`
         — eso indica que ya emitimos super/sub, no es título
    """
    letters = sum(1 for c in text if c.isalpha() and c.isascii())
    if letters < _HEADING_MIN_LETTERS:
        return False
    # Operadores que típicamente aparecen en tamaño "display" pero no son títulos
    display_operators = "∫∮∯∰∱∲∳∑∏∐"
    if any(c in display_operators for c in text):
        return False
    if "^{" in text or "_{" in text:
        return False
    return True


def _format_line(glyphs: list[_Glyph], body_size: float) -> str:
    """
    Convierte una línea (lista de glifos) a una línea Markdown.

    Si el tamaño máximo de la línea es > body * 1.4 → H1
                                     > body * 1.18 → H2
                                     > body * 1.08 → H3 (capturas pequeñas)
    Solo se promueve a heading si la línea contiene prosa real (≥4 letras).
    """
    if not glyphs:
        return ""
    text = _wrap_runs(glyphs, body_size=body_size)
    stripped = text.strip()
    if not stripped:
        return ""
    max_sz = _line_max_size(glyphs)
    if body_size > 0 and _looks_like_heading(stripped):
        ratio = max_sz / body_size
        if ratio >= 1.40:
            return f"# {stripped}"
        if ratio >= 1.18:
            return f"## {stripped}"
        if ratio >= 1.08:
            return f"### {stripped}"
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Reconstrucción de matrices (estructura 2D → \begin{bmatrix})
# ─────────────────────────────────────────────────────────────────────────────
#
# En PDFs LaTeX/Word las matrices se dibujan con piezas de corchete extensible
# (U+23A1..U+23AD, U+239B..U+23AD) que PyMuPDF expone como glifos en líneas
# propias. Detectamos un bloque "delimitado por piezas de corchete" y
# reconstruimos las filas interiores como filas de matriz. Esto evita el peor
# fallo: que una matriz `[1 1 2 | 9; ...]` colapse a la prosa como `1129`.

# Piezas de corchete/paréntesis/llave extensible — SOLO aparecen en layout
# matemático, así que su presencia es señal fuerte (bajo riesgo de falso positivo).
_BRACKET_PIECES = frozenset(
    "⎡⎢⎣⎤⎥⎦"   # corchetes extensibles
    "⎧⎨⎩⎪⎫⎬⎭"  # llaves extensibles
    "⎛⎜⎝⎞⎟⎠"   # paréntesis extensibles
)

# Hueco (fracción del tamaño de fuente) que separa celdas dentro de una fila de
# matriz. Mayor que un espacio de palabra (0.18) pero alcanzable por columnas.
_MATRIX_CELL_GAP = 0.9

# Ventana máxima de líneas que puede abarcar un bloque de matriz (evita unir
# brackets de zonas distintas de la página por error).
_MATRIX_MAX_SPAN = 14


def _bracket_ratio(glyphs: list[_Glyph]) -> float:
    """Fracción de glifos no-whitespace que son piezas de corchete extensible."""
    non_ws = [g for g in glyphs if not g.is_whitespace]
    if not non_ws:
        return 0.0
    n_bracket = sum(1 for g in non_ws if g.char in _BRACKET_PIECES)
    return n_bracket / len(non_ws)


def _is_bracket_line(glyphs: list[_Glyph]) -> bool:
    return _bracket_ratio(glyphs) >= 0.6


def _split_cells(glyphs: list[_Glyph]) -> list[str]:
    """
    Parte una fila de matriz en celdas por huecos horizontales, descartando las
    piezas de corchete. Cada celda se formatea con _wrap_runs (preserva sub/sup).
    Si la tipografía no deja huecos (matriz muy compacta) devuelve 1 celda — la
    estructura queda marcada aunque las columnas no se puedan separar.
    """
    data = [g for g in glyphs if g.char not in _BRACKET_PIECES]
    non_ws = [g for g in data if not g.is_whitespace]
    if not non_ws:
        return []
    line_size = max(g.size for g in non_ws)
    cell_gap = line_size * _MATRIX_CELL_GAP

    ordered = sorted(data, key=lambda g: g.bbox_x0)
    cells: list[list[_Glyph]] = []
    current: list[_Glyph] = []
    prev_x1: Optional[float] = None
    for g in ordered:
        if g.is_whitespace:
            continue
        if prev_x1 is not None and (g.bbox_x0 - prev_x1) > cell_gap and current:
            cells.append(current)
            current = []
        current.append(g)
        prev_x1 = g.bbox_x1
    if current:
        cells.append(current)
    return [_wrap_runs(c).strip() for c in cells if c]


def _detect_matrix_spans(lines: list[list[_Glyph]]) -> list[tuple[int, int]]:
    """
    Devuelve los rangos [start, end) de líneas que forman bloques de matriz.
    Un bloque empieza en una línea de corchete y termina en la siguiente línea
    de corchete dentro de _MATRIX_MAX_SPAN, con ≥1 fila de datos entre medio.
    """
    spans: list[tuple[int, int]] = []
    n = len(lines)
    i = 0
    while i < n:
        if _is_bracket_line(lines[i]):
            # Busca la línea de corchete de cierre dentro de la ventana.
            j = i + 1
            last_bracket = -1
            data_rows = 0
            while j < n and (j - i) <= _MATRIX_MAX_SPAN:
                if _is_bracket_line(lines[j]):
                    last_bracket = j
                elif any(not g.is_whitespace for g in lines[j]):
                    data_rows += 1
                j += 1
            if last_bracket > i and data_rows >= 1:
                spans.append((i, last_bracket + 1))
                i = last_bracket + 1
                continue
        i += 1
    return spans


def _render_matrix(block: list[list[_Glyph]]) -> str:
    """Renderiza un bloque de líneas como matriz LaTeX `bmatrix`."""
    rows: list[str] = []
    for line in block:
        if _is_bracket_line(line):
            continue  # las piezas de corchete no son datos
        cells = _split_cells(line)
        if cells:
            rows.append(" & ".join(cells))
    if not rows:
        return ""
    body = " \\\\\n".join(rows)
    return "$$\n\\begin{bmatrix}\n" + body + "\n\\end{bmatrix}\n$$"


def _render_page(lines: list[list[_Glyph]], body_size: float) -> list[str]:
    """
    Convierte las líneas de glifos de una página a líneas/bloques Markdown.

    Orquesta, en orden:
      1. De-fusión de columnas físicas (evita ecuaciones falsas).
      2. Detección y render de bloques de matriz (estructura 2D).
      3. Formateo línea-a-línea del resto (headings, sub/super, fracciones).
    """
    # 1. De-fusión de columnas: aplana cada línea en sus segmentos de columna.
    flat: list[list[_Glyph]] = []
    for line in lines:
        flat.extend(_split_columns(line))

    # 2. Identifica spans de matriz sobre las líneas ya de-fusionadas.
    spans = _detect_matrix_spans(flat)
    span_starts = {s: e for s, e in spans}

    out: list[str] = []
    idx = 0
    while idx < len(flat):
        if idx in span_starts:
            end = span_starts[idx]
            md = _render_matrix(flat[idx:end])
            if md:
                out.append(md)
                idx = end
                continue
        formatted = _format_line(flat[idx], body_size)
        if formatted:
            out.append(formatted)
        idx += 1
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Conversión principal
# ─────────────────────────────────────────────────────────────────────────────

def _build_font_recovery_if_needed(doc) -> "Optional[object]":
    """
    Instancia FontRecovery sólo si el documento contiene fuentes con CMap
    roto conocidas. Evita cargar las 150+ referencias en PDFs que no las
    necesitan (ahorra ~4s por PDF de prosa).
    """
    try:
        from . import font_recovery as _fr
    except ImportError:
        try:
            import font_recovery as _fr  # type: ignore
        except ImportError:
            return None

    # Detectar al menos UN font roto en el documento
    needs_it = False
    for page in doc:
        for f in page.get_fonts(full=False):
            if _fr._font_is_broken(f[3] if len(f) > 3 else ""):
                needs_it = True
                break
        if needs_it:
            break

    if not needs_it:
        return None

    try:
        return _fr.FontRecovery()
    except Exception as exc:
        logger.warning("stem_engine: FontRecovery init failed (%s) — sin recovery", exc)
        return None


def _build_glyph_name_recovery_if_needed(doc) -> "Optional[object]":
    """
    Instancia GlyphNameRecovery (canal DETERMINISTA por nombre de glifo) solo
    si el documento tiene fuentes con CMap rota. Carga el suplemento verificado.
    Devuelve None ante cualquier dificultad (el pipeline sigue con el visual).
    """
    try:
        from . import glyph_name_recovery as _gnr
        from . import font_recovery as _fr
    except ImportError:
        try:
            import glyph_name_recovery as _gnr  # type: ignore
            import font_recovery as _fr  # type: ignore
        except ImportError:
            return None

    needs_it = False
    for page in doc:
        for f in page.get_fonts(full=False):
            if _fr._font_is_broken(f[3] if len(f) > 3 else ""):
                needs_it = True
                break
        if needs_it:
            break
    if not needs_it:
        return None

    try:
        return _gnr.GlyphNameRecovery(doc, supplement=_gnr.load_supplement())
    except Exception as exc:
        logger.warning("stem_engine: GlyphNameRecovery init failed (%s) — sin canal de nombres", exc)
        return None


def _build_glyph_table_recovery_if_needed(doc) -> "Optional[object]":
    """
    Instancia GlyphTableRecovery (canal DETERMINISTA por tabla familia+codepoint)
    solo si el documento tiene fuentes con CMap rota. Carga el registro JSON.
    Devuelve None ante cualquier dificultad (el pipeline sigue sin este canal).
    """
    try:
        from . import font_table_recovery as _ftr
        from . import font_recovery as _fr
    except ImportError:
        try:
            import font_table_recovery as _ftr  # type: ignore
            import font_recovery as _fr  # type: ignore
        except ImportError:
            return None

    try:
        rec = _ftr.GlyphTableRecovery()
    except Exception as exc:
        logger.warning("stem_engine: GlyphTableRecovery init failed (%s) — sin canal de tabla", exc)
        return None
    # Si la tabla cargó vacía (archivo ausente/corrupto) no aporta nada.
    if rec.stats().get("entries_in_table", 0) == 0:
        return None

    # Activar si el doc tiene fuentes que la TABLA cubre (incluye blind-spots
    # que el regex de nombres no reconoce) o, por compatibilidad, fuentes con
    # CMap rota conocidas por el regex.
    for page in doc:
        for f in page.get_fonts(full=False):
            fname = f[3] if len(f) > 3 else ""
            if rec.covers(fname) or _fr._font_is_broken(fname):
                return rec
    return None


def convert_pdf_stem(content: bytes, filename: str, metadata: Optional[dict] = None) -> dict:
    """
    Pipeline STEM completo. Compatible con el contrato de pdf_engine:
    devuelve dict con success / markdown / char_count / word_count /
    engine_used / pdf_metadata.

    NO maneja excepciones internas: el caller (pdf_engine._route_pdf) las
    captura y cae al pipeline tradicional.
    """
    doc = fitz.open(stream=content, filetype="pdf")
    meta = metadata or {}
    if not meta:
        raw = doc.metadata or {}
        meta = {
            "title":    (raw.get("title") or "").strip(),
            "author":   (raw.get("author") or "").strip(),
            "subject":  (raw.get("subject") or "").strip(),
            "keywords": (raw.get("keywords") or "").strip(),
            "pages":    len(doc),
        }

    body_size = _estimate_body_size(doc)
    logger.debug("stem_engine: body_size estimado = %.1f pt", body_size)

    # Canal de nombres (determinista) + font_recovery (visual, fallback).
    # Solo se instancian si el documento tiene fuentes con CMap rota.
    glyph_name_recovery = _build_glyph_name_recovery_if_needed(doc)
    if glyph_name_recovery is not None:
        logger.info("stem_engine: glyph_name_recovery (determinista) ACTIVO para '%s'", filename)
    font_recovery = _build_font_recovery_if_needed(doc)
    if font_recovery is not None:
        logger.info("stem_engine: font_recovery (visual) ACTIVO para '%s'", filename)
    glyph_table_recovery = _build_glyph_table_recovery_if_needed(doc)
    if glyph_table_recovery is not None:
        logger.info("stem_engine: glyph_table_recovery (tabla) ACTIVO para '%s' (%d entradas)",
                    filename, glyph_table_recovery.stats().get("entries_in_table", 0))

    pages_md: list[str] = []
    for page in doc:
        lines = _extract_glyphs(page, font_recovery=font_recovery,
                                glyph_name_recovery=glyph_name_recovery,
                                glyph_table_recovery=glyph_table_recovery)
        page_lines = _render_page(lines, body_size)
        if page_lines:
            pages_md.append("\n".join(page_lines))

    raw_md = "\n\n".join(pages_md)

    # Pipeline de normalización post-extracción
    markdown = normalize_unicode(raw_md)

    # QA: auditoría de integridad de caracteres. Tras _sanitize_charset el
    # informe debe salir limpio; si no, lo registramos como WARNING para
    # diagnóstico (no debería ocurrir, es una invariante del pipeline).
    audit = audit_charset(markdown)
    if not audit["clean"]:
        logger.warning(
            "stem_engine: %d caracteres sospechosos sobrevivieron a la "
            "sanitización en '%s': %s",
            audit["total_suspect"], filename, audit["by_codepoint"],
        )

    # Etiqueta del motor: aditiva por canal que efectivamente recuperó algo.
    active_channels: list[str] = []
    if glyph_table_recovery is not None:
        tstats = glyph_table_recovery.stats()
        logger.info("stem_engine: glyph_table_recovery stats=%s", tstats)
        if tstats.get("resolved", 0) > 0:
            active_channels.append("glyph_table")
    if glyph_name_recovery is not None:
        gstats = glyph_name_recovery.stats()
        logger.info("stem_engine: glyph_name_recovery stats=%s", gstats)
        if gstats.get("resolved_agl", 0) + gstats.get("resolved_supplement", 0) > 0:
            active_channels.append("glyph_names")
    if font_recovery is not None:
        stats = font_recovery.stats()
        logger.info("stem_engine: font_recovery stats=%s", stats)
        if stats.get("matches_high", 0) + stats.get("matches_low", 0) + stats.get("grk_alpha_direct", 0) > 0:
            active_channels.append("font_recovery")
    engine_label = "+".join(["stem-analytical", *active_channels])

    if not markdown:
        return {
            "success":           False,
            "original_filename": filename,
            "error": "stem_engine: extracción vacía",
        }

    return {
        "success":           True,
        "original_filename": filename,
        "md_filename":       Path(filename).stem + ".md",
        "markdown":          markdown,
        "char_count":        len(markdown),
        "word_count":        len(markdown.split()),
        "pdf_metadata":      meta,
        "engine_used":       engine_label,
    }
