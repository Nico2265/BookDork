"""
=============================================================================
fidelity_metrics.py — Métricas de fidelidad PDF↔Markdown para conversión STEM
=============================================================================
Comparador formal entre el contenido de un PDF y el Markdown emitido por el
pipeline de conversión. Pensado para uso en CI (tests) y para introspección
puntual desde el endpoint admin.

Métricas devueltas:
  glyph_coverage      : fracción de glifos no-whitespace del PDF presentes en MD
  mojibake_score      : 1.0 si no hay U+FFFD, U+FFFE, ligaduras sin expandir
  symbol_preservation : ratio de símbolos matemáticos/griegos sobrevivientes
  structure_score     : heurística headings detectados / esperados
  super_sub_detection : presencia de marcadores ^/_ donde el PDF tenía glifos
                        de tamaño reducido
  overall             : promedio ponderado en [0, 1]

Estas métricas se calculan SIN ground truth externo — comparan PDF original
contra MD. Útiles para QA continuo: corres conversión, mides, sabes si
hubo regresión.
=============================================================================
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field, asdict
from typing import Optional

import fitz

logger = logging.getLogger("bookdork.fidelity_metrics")


# Símbolos cuya presencia en PDF debe verificarse en MD
_TRACKED_MATH = frozenset(
    "∫∮∑∏∂∇√≠≤≥≈±×÷⋅∗⊕⊗∈∉⊂⊃∪∩∀∃∞ℵℏ"
    "→←↔⇒⇐⇔↦"
    "αβγδεζηθικλμνξοπρστυφχψω"
    "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ"
)

# Indicadores de mojibake / extracción rota
_MOJIBAKE_CHARS = frozenset("�￾﻿\x00")
# Ligaduras tipográficas que NO deben aparecer en MD bien convertido
_UNEXPANDED_LIGATURES = frozenset("ﬀﬁﬂﬃﬄﬅﬆ")


@dataclass
class FidelityReport:
    glyph_coverage:      float
    mojibake_score:      float
    symbol_preservation: float
    structure_score:     float
    super_sub_detection: float
    equation_validity:   float
    overall:             float
    details:             dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


_HEADING_PAT = re.compile(r"^#{1,6}\s+", re.M)
_SUPER_SUB_PAT = re.compile(r"[\^_](?:\{[^}]+\}|[A-Za-z0-9])")


def _extract_pdf_text(doc) -> str:
    parts = []
    for page in doc:
        parts.append(page.get_text("text"))
    return "\n".join(parts)


def _count_smaller_glyphs(doc, sample_pages: int = 5) -> int:
    """Cuenta glifos cuyo tamaño es < 0.85 * tamaño máximo de su línea."""
    n = min(sample_pages, len(doc))
    smaller = 0
    for i in range(n):
        for block in doc[i].get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                line_size = max(s["size"] for s in spans)
                for span in spans:
                    if span["size"] < line_size * 0.85:
                        smaller += sum(1 for c in span["text"] if not c.isspace())
    return smaller


def compute(pdf_bytes: bytes, markdown: str,
            equation_validity: Optional[float] = None) -> FidelityReport:
    """
    Calcula el reporte de fidelidad. `equation_validity` se acepta como input
    externo (típicamente del módulo stem_validator) — si no se pasa, esta
    métrica recibe 1.0 (asumimos válido por defecto).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        pdf_text = _extract_pdf_text(doc)

        # ── 1. Glyph coverage ────────────────────────────────────────────────
        # Normalizamos ambos lados para que ligaduras expandidas en MD no
        # cuenten como "pérdida" de glifos. Comparamos el SET de chars
        # no-whitespace.
        pdf_chars = {c for c in pdf_text if not c.isspace()}
        md_chars = {c for c in markdown if not c.isspace()}
        # NFKC normaliza ligaduras a sus componentes ASCII
        md_chars_nfkc = {
            c for c in unicodedata.normalize("NFKC", markdown)
            if not c.isspace()
        }
        # Coverage = chars del PDF que aparecen en MD (raw o NFKC-normalizado)
        if pdf_chars:
            present = sum(
                1 for c in pdf_chars
                if c in md_chars or c in md_chars_nfkc
            )
            glyph_coverage = present / len(pdf_chars)
        else:
            glyph_coverage = 1.0

        # ── 2. Mojibake score ────────────────────────────────────────────────
        moji_hits = sum(1 for c in markdown if c in _MOJIBAKE_CHARS)
        ligature_hits = sum(1 for c in markdown if c in _UNEXPANDED_LIGATURES)
        bad_chars = moji_hits + ligature_hits
        if len(markdown) > 0:
            mojibake_score = max(0.0, 1.0 - (bad_chars / max(1, len(markdown) / 100)))
        else:
            mojibake_score = 1.0
        mojibake_score = min(1.0, max(0.0, mojibake_score))

        # ── 3. Symbol preservation ───────────────────────────────────────────
        pdf_symbols = {c for c in pdf_text if c in _TRACKED_MATH}
        md_symbols = {c for c in markdown if c in _TRACKED_MATH}
        if pdf_symbols:
            symbol_preservation = len(pdf_symbols & md_symbols) / len(pdf_symbols)
        else:
            symbol_preservation = 1.0

        # ── 4. Structure ─────────────────────────────────────────────────────
        # Esperamos al menos 1 heading si el PDF tiene > 200 caracteres
        md_headings = len(_HEADING_PAT.findall(markdown))
        if len(pdf_text) > 200:
            structure_score = 1.0 if md_headings >= 1 else 0.5
        else:
            structure_score = 1.0

        # ── 5. Super/sub detection ───────────────────────────────────────────
        # PDF tiene glifos de tamaño reducido → MD debería tener marcadores ^/_
        smaller_glyphs = _count_smaller_glyphs(doc)
        md_markers = len(_SUPER_SUB_PAT.findall(markdown))
        if smaller_glyphs >= 3:
            # Esperamos al menos algunos marcadores; el ratio exacto depende
            # de la naturaleza del PDF, así que medimos presencia + magnitud.
            expected_min = max(1, smaller_glyphs // 8)
            super_sub_detection = min(1.0, md_markers / expected_min)
        else:
            # No hay sub/super en el PDF — no penalizamos ausencia
            super_sub_detection = 1.0

        # ── 6. Equation validity (input externo) ─────────────────────────────
        eq_val = equation_validity if equation_validity is not None else 1.0

        # ── Overall ──────────────────────────────────────────────────────────
        weights = {
            "glyph_coverage":      0.20,
            "mojibake_score":      0.15,
            "symbol_preservation": 0.20,
            "structure_score":     0.10,
            "super_sub_detection": 0.25,
            "equation_validity":   0.10,
        }
        overall = (
            glyph_coverage      * weights["glyph_coverage"]
            + mojibake_score      * weights["mojibake_score"]
            + symbol_preservation * weights["symbol_preservation"]
            + structure_score     * weights["structure_score"]
            + super_sub_detection * weights["super_sub_detection"]
            + eq_val              * weights["equation_validity"]
        )

        details = {
            "pdf_chars_unique":    str(len(pdf_chars)),
            "md_chars_unique":     str(len(md_chars)),
            "mojibake_hits":       str(moji_hits),
            "unexpanded_ligatures": str(ligature_hits),
            "pdf_math_symbols":    str(len(pdf_symbols)),
            "md_math_symbols":     str(len(md_symbols)),
            "md_headings":         str(md_headings),
            "pdf_smaller_glyphs":  str(smaller_glyphs),
            "md_super_sub_markers": str(md_markers),
        }

        return FidelityReport(
            glyph_coverage=glyph_coverage,
            mojibake_score=mojibake_score,
            symbol_preservation=symbol_preservation,
            structure_score=structure_score,
            super_sub_detection=super_sub_detection,
            equation_validity=eq_val,
            overall=overall,
            details=details,
        )
    finally:
        doc.close()


def format_report(report: FidelityReport, label: str = "Fidelity") -> str:
    """Render bonito para CLI / logging."""
    lines = [
        "╔══════════════════════════════════════════════════════════════════╗",
        f"║  {label:<64s} ║",
        f"║  Overall: {report.overall*100:5.2f}%                                              ║",
        "╠══════════════════════════════════════════════════════════════════╣",
    ]
    metrics = [
        ("glyph_coverage",      report.glyph_coverage),
        ("mojibake_score",      report.mojibake_score),
        ("symbol_preservation", report.symbol_preservation),
        ("structure_score",     report.structure_score),
        ("super_sub_detection", report.super_sub_detection),
        ("equation_validity",   report.equation_validity),
    ]
    for name, val in metrics:
        bar = "█" * int(val * 20) + "░" * (20 - int(val * 20))
        lines.append(f"║  {name:<22s} {val*100:6.2f}%  {bar}      ║")
    lines.append("╠══════════════════════════════════════════════════════════════════╣")
    for k, v in report.details.items():
        lines.append(f"║    {k:<25s} = {v:<32s}║")
    lines.append("╚══════════════════════════════════════════════════════════════════╝")
    return "\n".join(lines)
