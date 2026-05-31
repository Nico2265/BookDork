"""
=============================================================================
font_table_recovery.py — Canal DETERMINISTA por tabla (familia, codepoint)
=============================================================================
Tercer canal de recuperación de glifos, ORTOGONAL a los otros dos:

  * glyph_name_recovery — lee el NOMBRE del glifo (/Differences) → AGL.
  * font_recovery       — matching VISUAL de siluetas (probabilístico).
  * font_table_recovery — TABLA curada `(familia, codepoint) → Unicode`  ← este.

POR QUÉ EXISTE
──────────────
Las fuentes-símbolo Type1 con encoding built-in roto (MathematicalPi-*) emiten
su glifo como un carácter ASCII IMPRIMIBLE incorrecto: el signo `=` sale como
`'m'`, `/` como `'Y'`, `∞` como `'@'`. Eso evade a los otros dos canales:

  * el de NOMBRES solo se dispara sobre glifos crudos de control (`\\x02`); `'m'`
    no lo es, y además el CFF subsetado no conserva nombre semántico (`cid00109`);
  * el VISUAL no se dispara (no es control) y, aunque se forzara, confunde
    siluetas casi idénticas (`=` ↔ `⇔`): medido 3/12 de acierto.

Resultado: ~10–15 mil glifos por libro pasaban SILENCIOSAMENTE — el peor tipo
de error ("engaña"). Esta tabla los resuelve sin ambigüedad.

PRINCIPIO: solo ground-truth verificado, jamás adivinar
───────────────────────────────────────────────────────
La tabla (`font_glyph_table.json`) solo contiene entradas verificadas por
render del PDF + contexto de uso y/o consenso multi-documento. Si una
(familia, codepoint) no está en la tabla, `resolve` devuelve None (abstención):
el glifo sigue su curso y, si nada lo recupera, acaba como marcador visible.
Nunca se emite un mapeo conjeturado.

SEGURIDAD POR CONSTRUCCIÓN
──────────────────────────
La clave es el codepoint que PyMuPDF EXTRAE, que depende de cómo cada libro
embebe la fuente (mismo glifo puede salir como `0x59` en un libro y como un
control en otro, vía Identity-H). Por eso la tabla NUNCA mis-mapea otro libro:
si un libro no emite ese codepoint exacto para esa familia, la entrada
simplemente no aplica. La cobertura es por-encoding; la corrección es absoluta.

Dependencias: ninguna (stdlib). No importa fitz, numpy ni los otros módulos
(evita ciclos de import: este módulo lo consume stem_engine).
=============================================================================
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger("bookdork.font_table_recovery")

# Canonicalización de familia: idéntica semántica a font_harvester.canonical_font_key,
# replicada aquí para no crear un ciclo de import (font_harvester importa stem_engine).
_SUBSET_RE = re.compile(r"^[A-Z]{6}\+")
_CMAP_SUFFIX_RE = re.compile(r"-(Identity-[HV]|UCS2|UTF16|GBK-EUC-[HV])$")

_DEFAULT_TABLE_PATH = Path(__file__).with_name("font_glyph_table.json")


def canonical_family(name: Optional[str]) -> str:
    """'KKBOMG+MathematicalPi-Five-Identity-H' → 'MathematicalPi-Five'."""
    if not name:
        return ""
    n = _SUBSET_RE.sub("", name)
    n = _CMAP_SUFFIX_RE.sub("", n)
    return n


def load_table(path: "Optional[Path]" = None) -> dict:
    """
    Carga el registro JSON y lo compila a `{familia: {codepoint_int: unicode}}`.
    Robusto: ante archivo ausente/corrupto devuelve {} (el canal se abstiene de
    todo, sin romper el pipeline). Ignora entradas sin `unicode` no vacío.
    """
    p = path or _DEFAULT_TABLE_PATH
    try:
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("font_table_recovery: tabla no encontrada en %s — canal vacío", p)
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("font_table_recovery: tabla ilegible (%s) — canal vacío", exc)
        return {}

    compiled: dict = {}
    for family, entries in (raw.get("fonts") or {}).items():
        fam = canonical_family(family)
        cp_map: dict = {}
        for hexcp, info in (entries or {}).items():
            try:
                cp = int(hexcp, 16)
            except (ValueError, TypeError):
                logger.debug("font_table_recovery: codepoint inválido %r en %s", hexcp, family)
                continue
            u = (info or {}).get("unicode") if isinstance(info, dict) else info
            if u:                       # ignora vacío/None → abstención
                cp_map[cp] = u
        if cp_map:
            compiled[fam] = cp_map
    return compiled


class GlyphTableRecovery:
    """
    Resolución determinista por tabla. Stateless salvo estadísticas. Una
    instancia por documento (consistente con los otros canales).
    """

    def __init__(self, table: Optional[dict] = None) -> None:
        self._table = table if table is not None else load_table()
        self._stats = {
            "fonts_in_table": len(self._table),
            "entries_in_table": sum(len(v) for v in self._table.values()),
            "resolve_calls": 0,
            "resolved": 0,
            "abstained": 0,
        }

    def resolve(self, font_name: str, codepoint: int) -> Optional[str]:
        """
        Devuelve el Unicode curado para (familia(font_name), codepoint), o None
        (abstención) si no está en la tabla. Determinista, sin efectos visuales.
        """
        self._stats["resolve_calls"] += 1
        cp_map = self._table.get(canonical_family(font_name))
        if cp_map is not None:
            u = cp_map.get(codepoint)
            if u is not None:
                self._stats["resolved"] += 1
                return u
        self._stats["abstained"] += 1
        return None

    def covers(self, font_name: str) -> bool:
        """¿La tabla tiene alguna entrada para esta familia? (gate barato)."""
        return canonical_family(font_name) in self._table

    def stats(self) -> dict:
        return dict(self._stats)
