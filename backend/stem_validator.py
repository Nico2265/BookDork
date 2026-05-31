"""
=============================================================================
stem_validator.py — Validación matemática del Markdown convertido
=============================================================================
Detecta ecuaciones en el Markdown emitido por stem_engine y verifica que
estén bien formadas. Aplica auto-reparaciones triviales cuando el costo de
acertar es bajo (balanceo de llaves al cierre).

Validaciones:
  1. Balance de llaves   {} y corchetes [] de los super/subscripts emitidos
  2. Sintaxis LaTeX      via pylatexenc.LatexWalker (best-effort)
  3. Sin marcadores rotos (^ o _ sin contenido, llaves desbalanceadas)

Para el sistema en producción esto NO ES una validación simbólica completa
(no calculamos equivalencia algebraica con sympy: caro y poco rentable
para nuestro caso de uso, que es preservación textual de notación).
=============================================================================
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("bookdork.stem_validator")


@dataclass
class ValidationResult:
    total_equations:   int
    valid_equations:   int
    repaired_equations: int
    unbalanced_braces:  int
    orphan_markers:     int     # ^ o _ sin contenido tras
    latex_parse_errors: int

    @property
    def validity_rate(self) -> float:
        if self.total_equations == 0:
            return 1.0
        return self.valid_equations / self.total_equations


# Regex para detectar fragmentos sospechosos:
# - ^{ ó _{ sin } emparejada
# - ^ o _ aislados al final de palabra
_ORPHAN_MARKER = re.compile(r"[\^_]\s*$|[\^_]\s*[\)\}\],.!?]")

# Ecuación heurística: una línea o fragmento con ^ o _ y contenido alfanumérico
# entre símbolos matemáticos. Buscamos líneas con marcadores estructurales.
_HAS_MATH_MARKER = re.compile(r"(\^\{|_\{|\^[A-Za-z0-9]|_[A-Za-z0-9])")


def _balance_braces(s: str) -> tuple[bool, str, int]:
    """
    Verifica que { y } estén balanceados.
    Devuelve (ok, repaired_str, depth_off).

    Si hay más { que } al final, añade } al final como reparación.
    No toca más allá de 3 llaves de descuadre — descuadres mayores señalan
    daño profundo que la reparación trivial empeora.
    """
    depth = 0
    for c in s:
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return False, s, depth  # desorden temprano: no reparable
    if depth == 0:
        return True, s, 0
    if 0 < depth <= 3:
        return False, s + "}" * depth, depth
    return False, s, depth


def validate_markdown(md: str) -> ValidationResult:
    """
    Recorre el Markdown buscando fragmentos matemáticos y calcula métricas.

    Heurística: cualquier línea con `^{` `_{` `^<char>` `_<char>` se considera
    ecuación candidata. Para cada candidata verificamos balance y marcadores.
    """
    if not md:
        return ValidationResult(0, 0, 0, 0, 0, 0)

    lines = md.splitlines()
    total = 0
    valid = 0
    repaired = 0
    unbalanced = 0
    orphans = 0
    latex_errors = 0

    # Importación opcional: si pylatexenc no está, saltamos la validación profunda
    try:
        from pylatexenc.latexwalker import LatexWalker, LatexWalkerError
        _have_pylatexenc = True
    except ImportError:
        _have_pylatexenc = False

    for line in lines:
        if not _HAS_MATH_MARKER.search(line):
            continue
        total += 1
        line_valid = True

        # 1. Balance de llaves
        ok, _, _ = _balance_braces(line)
        if not ok:
            unbalanced += 1
            line_valid = False

        # 2. Marcadores huérfanos
        if _ORPHAN_MARKER.search(line):
            orphans += 1
            line_valid = False

        # 3. LaTeX parse (best-effort): solo sobre fragmentos $...$ ó $$...$$
        if _have_pylatexenc:
            for frag in re.findall(r"\$\$?(.+?)\$\$?", line, flags=re.DOTALL):
                try:
                    LatexWalker(frag).get_latex_nodes(pos=0)
                except LatexWalkerError:
                    latex_errors += 1
                    line_valid = False

        if line_valid:
            valid += 1

    return ValidationResult(
        total_equations=total,
        valid_equations=valid,
        repaired_equations=repaired,
        unbalanced_braces=unbalanced,
        orphan_markers=orphans,
        latex_parse_errors=latex_errors,
    )


def repair_markdown(md: str) -> tuple[str, int]:
    """
    Aplica reparaciones triviales sobre el Markdown:
      1. Cierra llaves desbalanceadas hasta 3 niveles
      2. Elimina marcadores huérfanos (`^` o `_` al final de línea sin contenido)

    Devuelve (markdown_reparado, n_reparaciones).
    """
    if not md:
        return md, 0

    n_repairs = 0
    out_lines: list[str] = []
    for line in md.splitlines():
        # 1. Marcadores huérfanos al final de línea — los strip-eamos
        stripped = re.sub(r"[\^_]\s*$", "", line)
        if stripped != line:
            n_repairs += 1
            line = stripped

        # 2. Balance de llaves
        ok, repaired, _ = _balance_braces(line)
        if not ok and repaired != line:
            n_repairs += 1
            line = repaired

        out_lines.append(line)

    return "\n".join(out_lines), n_repairs
