"""
=============================================================================
glyph_name_recovery.py — Canal DETERMINISTA de identidad por nombre de glifo
=============================================================================
Recupera/verifica la identidad de un glifo leyendo su NOMBRE (no su forma
visual ni su ToUnicode) y resolviéndolo a Unicode mediante la Adobe Glyph List.

POR QUÉ EXISTE ESTE CANAL
─────────────────────────
Muchos PDFs académicos embeben fuentes con ToUnicode CMap rota (MathematicalPi,
Euclid, etc.). PyMuPDF entonces emite el código de glifo crudo (`\\x02`). El
módulo hermano `font_recovery.py` resuelve eso por *matching visual* (siluetas),
que es probabilístico. Este módulo ataca el mismo problema por una vía
ORTOGONAL y determinista: el diccionario de fuente del PDF casi siempre conserva
el NOMBRE del glifo en su array `/Encoding /Differences` (p.ej. el código 2 está
declarado como `/summation`). El nombre es semántica explícita, no una conjetura.

QUÉ APORTA SOBRE LO QUE MuPDF YA HACE (medido empíricamente)
────────────────────────────────────────────────────────────
MuPDF ya resuelve por su cuenta los nombres AGL-estándar y la forma `uniXXXX`.
Este canal añade, de forma determinista:

  1. Forma `uXXXXXX` (4–6 hex). MuPDF la TRUNCA a 4 dígitos: `u1D6FC` (𝛼,
     U+1D6FC, Mathematical Italic Small Alpha) lo emite como U+D6FC (basura).
     TODO el bloque Mathematical Alphanumeric Symbols (U+1D400–1D7FF: negritas,
     itálicas, blackboard 𝕩, fraktur) vive en ese rango → crítico en STEM.
  2. Un SUPLEMENTO verificado para convenciones de nombres no-AGL por familia
     (p.ej. los nombres `H11xxx` de MathematicalPi), cargado desde JSON externo.
  3. Un canal de VERIFICACIÓN independiente para cruzar contra el matcher visual
     (consenso) y, sobre todo, para ABSTENERSE cuando el nombre es desconocido.

PRINCIPIO DE DISEÑO: fallar fuerte, jamás en silencio
─────────────────────────────────────────────────────
Coherente con el objetivo de 99.9% en contenido de sensibilidad extrema: si un
nombre de glifo no puede resolverse con certeza, el canal NO adivina — devuelve
None (abstención) y registra el nombre en `unresolved_names` para que pueda
construirse un suplemento empírico (ver bench_glyph_name_recovery.py --harvest).

ALCANCE v1
──────────
  * Fuentes simples (Type1/TrueType) con `/Encoding /Differences`  → soportado.
  * Encoding base sin Differences (builtin del programa de fuente) → best-effort.
  * Fuentes Type0/CID (Identity-H + CIDToGIDMap)                   → fuera de v1.

Dependencias: PyMuPDF (fitz), fontTools (fontTools.agl). Sin numpy, sin ML.
=============================================================================
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("bookdork.glyph_name_recovery")

# ─────────────────────────────────────────────────────────────────────────────
# Resolución de un nombre de glifo a Unicode
# ─────────────────────────────────────────────────────────────────────────────

# Fuentes de resolución, en orden de prioridad. Cada una es DETERMINISTA.
SOURCE_AGL = "agl"               # Adobe Glyph List (estándar + uni/u algorítmico)
SOURCE_SUPPLEMENT = "supplement" # tabla verificada por familia (no-AGL)

# Nombres que explícitamente NO portan semántica → abstención inmediata.
# `.notdef` es el glifo "sin definición"; los índices puros (`gNN`, `glyphNN`,
# `indexNN`, `cidNN`) son artefactos de subsetting sin significado público.
_SEMANTIC_VOID = re.compile(
    r"^(\.notdef|\.null|nonmarkingreturn|g\d+|glyph\d+|index\d+|cid\d+|c\d+)$"
)


@dataclass(frozen=True)
class Resolution:
    """Resultado de resolver un nombre de glifo."""
    char: str          # uno o más codepoints Unicode (las ligaduras dan len>1)
    source: str        # SOURCE_AGL | SOURCE_SUPPLEMENT
    name: str          # el nombre de glifo original (para auditoría/log)

    @property
    def is_deterministic(self) -> bool:
        # Ambas fuentes son deterministas; el campo existe para que la lógica
        # de consenso aguas abajo pueda ponderar el canal sin acoplarse a su
        # implementación interna.
        return True


def _strip_name(name: str) -> str:
    """Quita el '/' líder y espacios. No altera el resto del nombre."""
    return name.lstrip("/").strip()


def resolve_glyph_name(
    name: str,
    supplement: Optional[dict] = None,
) -> Optional[Resolution]:
    """
    Resuelve un nombre de glifo a Unicode de forma determinista.

    Orden (de mayor a menor canonicidad):
      1. Adobe Glyph List vía fontTools.agl.toUnicode — cubre nombres estándar,
         AGLFN, `uniXXXX` y `uXXXXXX` (corrige el truncamiento de MuPDF).
      2. Suplemento verificado por familia (nombres no-AGL como `H11001`).

    Devuelve None (ABSTENCIÓN) si el nombre no porta semántica o no se conoce.
    Nunca adivina.

    Args:
      name:        nombre de glifo (con o sin '/' líder).
      supplement:  dict opcional {nombre_sin_slash: "carácter_unicode"}.
    """
    if not name:
        return None
    clean = _strip_name(name)
    if not clean or _SEMANTIC_VOID.match(clean):
        return None

    # 1. Adobe Glyph List (determinista por especificación).
    try:
        from fontTools import agl
        uni = agl.toUnicode(clean)  # devuelve '' si no reconoce el nombre
    except Exception as exc:  # pragma: no cover - fontTools ausente o roto
        logger.debug("glyph_name_recovery: agl.toUnicode falló para %r: %s", clean, exc)
        uni = ""
    if uni:
        return Resolution(char=uni, source=SOURCE_AGL, name=clean)

    # 2. Suplemento verificado (sólo nombres que un humano validó).
    if supplement:
        mapped = supplement.get(clean)
        if mapped:
            return Resolution(char=mapped, source=SOURCE_SUPPLEMENT, name=clean)

    # 3. Abstención: nombre desconocido. No se inventa nada.
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Carga del suplemento verificado
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_SUPPLEMENT_PATH = Path(__file__).with_name("glyph_name_supplement.json")


def load_supplement(path: Optional[Path] = None) -> dict:
    """
    Carga el suplemento de nombres no-AGL desde JSON.

    Formato del JSON:
        {
          "_meta": { ... ignorado ... },
          "names": { "H11001": "+", "H11002": "−", ... }
        }

    Sólo deben añadirse entradas VERIFICADAS visualmente contra el glifo real.
    Una entrada equivocada aquí es justo el "error que engaña" que el pipeline
    debe evitar; por eso el archivo se versiona y se revisa como código.

    Devuelve {} si el archivo no existe o está vacío (no es un error).
    """
    p = path or _DEFAULT_SUPPLEMENT_PATH
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("glyph_name_recovery: suplemento ilegible (%s): %s", p, exc)
        return {}
    names = data.get("names", {})
    if not isinstance(names, dict):
        logger.warning("glyph_name_recovery: 'names' no es objeto en %s", p)
        return {}
    # Sanitiza claves (sin slash) y descarta valores no-string.
    clean = {}
    for k, v in names.items():
        if isinstance(k, str) and isinstance(v, str) and v:
            clean[_strip_name(k)] = v
    return clean


# ─────────────────────────────────────────────────────────────────────────────
# Lectura de nombres de glifo desde el diccionario de fuente del PDF
# ─────────────────────────────────────────────────────────────────────────────

# Tokeniza un array PDF `/Differences`: enteros (resetean el código actual) y
# nombres `/foo` (se asignan al código actual; luego el código se incrementa).
# Los nombres PDF admiten muchos caracteres salvo whitespace y delimitadores.
_DIFF_TOKEN = re.compile(r"(\d+)|/([^\s/\[\]<>(){}]+)")


def parse_differences(array_str: str) -> dict[int, str]:
    """
    Convierte el texto de un array `/Differences` en {código: nombre}.

    Semántica PDF (ISO 32000-1 §9.6.6.1): un entero fija el código de inicio;
    cada nombre subsiguiente se asigna a ese código y luego incrementa en 1.

    Ejemplo:
        "[ 2 /summation 3 /integral /alpha ]"
        → {2: "summation", 3: "integral", 4: "alpha"}
    """
    out: dict[int, str] = {}
    code: Optional[int] = None
    for m in _DIFF_TOKEN.finditer(array_str or ""):
        num, nm = m.group(1), m.group(2)
        if num is not None:
            code = int(num)
        elif nm is not None and code is not None:
            out[code] = nm
            code += 1
    return out


# Tipos de subtipo de fuente que tratamos como "simples" (code = byte directo).
_SIMPLE_SUBTYPES = {"/Type1", "/TrueType", "/MMType1", "/Type3"}


@dataclass
class _FontNameMap:
    """Mapa code→nombre de una fuente, con metadatos de diagnóstico."""
    code_to_name: dict[int, str] = field(default_factory=dict)
    subtype: str = ""
    source: str = ""           # "differences" | "program" | "" (ninguno)
    has_tounicode: bool = False


class GlyphNameRecovery:
    """
    Canal determinista de recuperación por nombre de glifo. Stateful por
    documento: cachea el mapa code→nombre por xref de fuente.

    Uso típico:
        gnr = GlyphNameRecovery(doc, supplement=load_supplement())
        res = gnr.resolve(font_xref, code)   # Resolution | None
        if res is not None:
            char = res.char                  # determinista
        # else: abstención → dejar al matcher visual / marcar incertidumbre
    """

    def __init__(self, doc, supplement: Optional[dict] = None) -> None:
        self._doc = doc
        self._supplement = supplement if supplement is not None else load_supplement()
        self._maps: dict[int, _FontNameMap] = {}   # xref → mapa
        self._stats = {
            "fonts_seen":         0,
            "fonts_with_names":   0,  # fuentes de las que extrajimos ≥1 nombre
            "resolve_calls":      0,
            "resolved_agl":       0,
            "resolved_supplement": 0,
            "abstained_no_name":  0,  # el código no tenía nombre declarado
            "abstained_unknown":  0,  # tenía nombre pero no se pudo resolver
        }
        # Nombres que aparecieron pero no se resolvieron: insumo para construir
        # el suplemento empíricamente (clave de la metodología QA).
        self._unresolved: dict[str, int] = {}

    # ── extracción del mapa code→nombre ───────────────────────────────────────
    def _xref_get(self, xref: int, key: str) -> tuple[str, str]:
        """Wrapper defensivo de doc.xref_get_key → (kind, value)."""
        try:
            return self._doc.xref_get_key(xref, key)
        except Exception as exc:
            logger.debug("glyph_name_recovery: xref_get_key(%s,%s) falló: %s", xref, key, exc)
            return ("null", "null")

    def _resolve_indirect(self, value: str) -> Optional[int]:
        """De 'N 0 R' devuelve N; None si no es referencia indirecta."""
        m = re.match(r"\s*(\d+)\s+\d+\s+R\s*$", value or "")
        return int(m.group(1)) if m else None

    def _read_differences(self, font_xref: int) -> dict[int, str]:
        """
        Extrae {code: nombre} del `/Encoding /Differences` de una fuente.
        El `/Encoding` puede ser:
          * indirecto ('N 0 R')      → seguir y leer Differences de ahí
          * dict inline ('<< ... >>')→ Differences embebido
          * name ('/WinAnsiEncoding')→ encoding base, sin Differences
          * ausente                  → {}
        """
        kind, value = self._xref_get(font_xref, "Encoding")
        if kind == "null":
            return {}

        # Caso indirecto: el Encoding es su propio objeto.
        enc_xref = self._resolve_indirect(value) if kind == "xref" else None
        if enc_xref is not None:
            dkind, dval = self._xref_get(enc_xref, "Differences")
            if dkind == "array":
                return parse_differences(dval)
            return {}

        # Caso dict inline: leer Differences directamente de la fuente con la
        # ruta anidada 'Encoding/Differences'.
        if kind == "dict":
            dkind, dval = self._xref_get(font_xref, "Encoding/Differences")
            if dkind == "array":
                return parse_differences(dval)

        # Caso name (encoding base) o cualquier otro: sin Differences.
        return {}

    def _read_program_encoding(self, font_xref: int) -> dict[int, str]:
        """
        Best-effort: cuando no hay Differences, intenta leer el vector
        /Encoding del programa de fuente Type1 embebido (mapea code→nombre).
        Devuelve {} ante cualquier dificultad — nunca lanza.
        """
        try:
            extracted = self._doc.extract_font(font_xref)
        except Exception as exc:
            logger.debug("glyph_name_recovery: extract_font(%s) falló: %s", font_xref, exc)
            return {}
        if not extracted or len(extracted) < 4:
            return {}
        _basefont, ext, _ftype, buf = extracted[0], extracted[1], extracted[2], extracted[3]
        if not buf:
            return {}
        ext = (ext or "").lower()
        try:
            if ext in ("pfa", "pfb", "t1"):
                from fontTools import t1Lib
                import io
                # t1Lib espera ruta/archivo; usamos un buffer en memoria.
                t1 = t1Lib.T1Font()
                t1.data = bytes(buf)
                t1.parse()
                enc = t1.font.get("Encoding")
                if isinstance(enc, list):
                    return {
                        i: n for i, n in enumerate(enc)
                        if isinstance(n, str) and n != ".notdef"
                    }
        except Exception as exc:
            logger.debug("glyph_name_recovery: lectura de programa Type1 falló (%s): %s", font_xref, exc)
        return {}

    def _name_map(self, font_xref: int) -> _FontNameMap:
        """Construye (y cachea) el mapa code→nombre de una fuente."""
        cached = self._maps.get(font_xref)
        if cached is not None:
            return cached

        self._stats["fonts_seen"] += 1
        # xref_get_key devuelve (kind, value); el subtipo es el VALUE ('/Type1').
        _, subtype = self._xref_get(font_xref, "Subtype")
        tk_kind, _ = self._xref_get(font_xref, "ToUnicode")
        fm = _FontNameMap(subtype=subtype, has_tounicode=(tk_kind != "null"))

        # Sólo fuentes simples: en CID/Type0 el "code" no es el byte directo.
        if subtype in _SIMPLE_SUBTYPES:
            diff = self._read_differences(font_xref)
            if diff:
                fm.code_to_name = diff
                fm.source = "differences"
            else:
                prog = self._read_program_encoding(font_xref)
                if prog:
                    fm.code_to_name = prog
                    fm.source = "program"

        if fm.code_to_name:
            self._stats["fonts_with_names"] += 1
        self._maps[font_xref] = fm
        return fm

    # ── API pública ──────────────────────────────────────────────────────────
    def resolve(self, font_xref: int, code: int) -> Optional[Resolution]:
        """
        Resuelve (fuente, código de glifo) → Resolution determinista, o None
        (abstención). `code` es el byte/codepoint crudo que emite el extractor
        (para fuentes simples coincide con `ord(char)` de PyMuPDF).
        """
        self._stats["resolve_calls"] += 1
        fm = self._name_map(font_xref)
        name = fm.code_to_name.get(code)
        if not name:
            self._stats["abstained_no_name"] += 1
            return None

        res = resolve_glyph_name(name, self._supplement)
        if res is None:
            self._stats["abstained_unknown"] += 1
            self._unresolved[name] = self._unresolved.get(name, 0) + 1
            logger.debug("glyph_name_recovery: nombre no resuelto %r (font xref=%s code=%s)",
                         name, font_xref, code)
            return None

        if res.source == SOURCE_AGL:
            self._stats["resolved_agl"] += 1
        else:
            self._stats["resolved_supplement"] += 1
        return res

    def name_for(self, font_xref: int, code: int) -> Optional[str]:
        """
        Devuelve el NOMBRE de glifo declarado para (fuente, código), o None.
        Útil para marcar de forma diagnóstica un glifo que tiene nombre pero
        que no se pudo resolver a Unicode (p.ej. ⟦?H17010⟧) en vez de borrarlo.
        """
        return self._name_map(font_xref).code_to_name.get(code)

    def font_diagnostics(self, font_xref: int) -> _FontNameMap:
        """Devuelve el mapa/metadatos de una fuente (para QA y benchmark)."""
        return self._name_map(font_xref)

    def stats(self) -> dict:
        return dict(self._stats)

    def unresolved_names(self) -> dict[str, int]:
        """Nombres vistos pero no resueltos, con su frecuencia. Para --harvest."""
        return dict(self._unresolved)
