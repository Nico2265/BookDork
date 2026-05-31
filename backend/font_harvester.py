"""
=============================================================================
font_harvester.py — Inventario de fuentes rotas en un corpus de PDFs (Fase 1)
=============================================================================
NO convierte ni recupera nada. MIDE. Responde la pregunta que des-arriesga
todo el proyecto del "diccionario de caracteres matemáticos":

    ¿Cuántas FAMILIAS de fuente con CMap roto existen realmente en el corpus,
    y cómo se distribuye su frecuencia?

Si la respuesta es "decenas con cola Zipf" (hipótesis), curar el diccionario
es un proyecto acotado. Si fueran "miles uniformes", sería inviable. Este
módulo entrega el número, no la opinión.

QUÉ MIDE, POR GLIFO
───────────────────
Para cada (familia_fuente, codepoint) emitido por PyMuPDF clasifica el riesgo
REUTILIZANDO los predicados del propio motor (font_recovery / stem_engine),
de modo que mide EXACTAMENTE lo que el motor falla, no una aproximación:

  * silent_mismap   — imprimible-ASCII desde fuente sospechosa: corrupción
                      SILENCIOSA (un `=` que sale como `m`). El objetivo #1.
  * markered_control— control/PUA desde fuente rota: el motor lo marca
                      `⟦?…⟧` o lo recupera. Falla fuerte, visible.
  * recovered_grk   — letra ASCII desde fuente Grk: ya cubierto por el mapa
                      fonético de font_recovery.
  * ok              — todo lo demás.

DETECTOR DE PUNTOS CIEGOS (valor QA principal)
──────────────────────────────────────────────
El motor hoy decide "fuente rota" por un regex de nombres
(font_recovery._BROKEN_FONT_PATTERNS). Eso es FRÁGIL: una fuente Type0 con
/Encoding Identity-H y SIN /ToUnicode emite Unicode basura SIN IMPORTAR su
nombre. Este módulo marca esas fuentes como `structural_suspect` aunque el
regex no las reconozca, y reporta el conjunto:

    structural_suspect AND NOT name_pattern_broken

= fuentes que corrompen en silencio y que el motor ni siquiera sabe que existen.

SALIDAS
───────
  1. Informe humano (stdout / --report-out): ranking de fuentes por glifos
     silenciosos, curva de cobertura Zipf, y lista de puntos ciegos.
  2. Inventario JSON (--json-out): worklist de curación para la Fase 2 —
     por familia sospechosa, cada codepoint con (char emitido, frecuencia,
     riesgo), listo para que un humano rellene el Unicode objetivo.

USO
───
    python -X utf8 "backend/font_harvester.py" CORPUS_DIR \
        --json-out inventory.json --report-out report.txt
    python -X utf8 "backend/font_harvester.py" libro.pdf

Dependencias: PyMuPDF (fitz). Sin numpy, sin ML.
=============================================================================
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import fitz  # PyMuPDF

# Reutilizamos los predicados EXACTOS del motor para no medir una aproximación.
try:
    from . import font_recovery as _fr
    from . import stem_engine as _se
except ImportError:  # ejecución directa, sin paquete
    import font_recovery as _fr  # type: ignore
    import stem_engine as _se  # type: ignore

logger = logging.getLogger("bookdork.font_harvester")

# ─────────────────────────────────────────────────────────────────────────────
# Canonicalización de nombres de fuente
# ─────────────────────────────────────────────────────────────────────────────

# Prefijo de subset que el productor de PDF antepone: "ABCDEF+RealName".
_SUBSET_RE = re.compile(r"^[A-Z]{6}\+")
# Sufijos de CMap que cuelgan del BaseFont en fuentes Type0 ("Name-Identity-H").
# Solo formas conocidas — NO recortar "-H"/"-V" genéricos para no comerse
# nombres legítimos (p.ej. "...-Bold").
_CMAP_SUFFIX_RE = re.compile(r"-(Identity-[HV]|UCS2|UTF16|GBK-EUC-[HV])$")


def canonical_font_key(name: Optional[str]) -> str:
    """
    Familia canónica de una fuente: sin prefijo de subset ni sufijo de CMap.

    'KKBOMG+MathematicalPi-Five'        → 'MathematicalPi-Five'
    'MathematicalPi-Five-Identity-H'    → 'MathematicalPi-Five'
    'ABCDEF+Times-Italic'               → 'Times-Italic'
    Permite unir las stats del rawdict (basefont del descendiente) con la
    metadata de get_fonts (basefont del Type0 wrapper), que difieren en esos
    dos adornos.
    """
    if not name:
        return ""
    n = _SUBSET_RE.sub("", name)
    n = _CMAP_SUFFIX_RE.sub("", n)
    return n


# ─────────────────────────────────────────────────────────────────────────────
# Clasificación de riesgo por glifo (lógica pura, testeable sin PDF)
# ─────────────────────────────────────────────────────────────────────────────

# Categorías de riesgo. Strings (no Enum) para serializar JSON sin fricción.
SILENT_MISMAP = "silent_mismap"
MARKERED_CONTROL = "markered_control"
RECOVERED_GRK = "recovered_grk"
STRAY_CONTROL = "stray_control"   # control en fuente NO sospechosa (raro)
OK = "ok"


def _is_printable_ascii(ch: str) -> bool:
    """¿`ch` es un carácter ASCII imprimible (0x21–0x7E, sin espacio)?"""
    return len(ch) == 1 and 0x21 <= ord(ch) <= 0x7E


def glyph_risk(ch: str, font_is_suspect: bool, is_grk: bool) -> str:
    """
    Clasifica el riesgo de un glifo emitido. Espeja la lógica del motor:

      * control/PUA  → el motor lo trata (recupera o marca `⟦?…⟧`). Visible.
      * Grk + letra  → font_recovery lo mapea fonéticamente.
      * imprimible-ASCII desde fuente sospechosa → SILENCIOSO (el objetivo).

    `font_is_suspect` = name_pattern_broken OR structural_suspect (ver más
    abajo). Se inyecta ya resuelto para mantener esta función pura.
    """
    if not ch:
        return OK
    if _se._is_raw_broken_glyph(ch):              # control C0/C1, DEL, PUA
        return MARKERED_CONTROL if font_is_suspect else STRAY_CONTROL
    if _is_printable_ascii(ch):
        if is_grk and ch.isalpha():
            return RECOVERED_GRK
        if font_is_suspect:
            return SILENT_MISMAP
    return OK


# ─────────────────────────────────────────────────────────────────────────────
# Metadata estructural por fuente
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FontMeta:
    """Identidad estructural de una familia de fuente en un documento."""
    family: str
    subtype: str = ""           # Type0 / Type1 / TrueType / Type3 …
    encoding: str = ""          # Identity-H / WinAnsiEncoding / "" …
    has_tounicode: bool = False
    name_pattern_broken: bool = False   # lo que el regex del motor detecta hoy

    @property
    def is_identity_type0(self) -> bool:
        return self.subtype.startswith("Type0") and "Identity" in (self.encoding or "")


# Fracción máxima de letras (sobre imprimible-ASCII) para seguir considerando
# sospechosa una fuente Type0/Identity. Prosa legible ronda ~0.90+ (letras
# formando palabras); una fuente-símbolo rota emite mayoría de signos/dígitos.
# Calibrado contra Stewart: AGaramond/Times-Bold (texto, ~0.95) quedan FUERA;
# fuentes-símbolo (~<0.4) quedan dentro.
_STRUCT_ALPHA_MAX = 0.60


def is_structural_suspect(meta: FontMeta, emits_printable_ascii: bool,
                          alpha_ratio: float = 0.0) -> bool:
    """
    ¿La ESTRUCTURA de la fuente la hace sospechosa, con independencia de su
    nombre? Caso base:

      Type0 + Identity-H + SIN /ToUnicode + emite imprimible-ASCII

    PERO eso NO basta: un Type0/Identity sin ToUnicode a menudo decodifica
    correctamente vía el cmap/charset embebido (típico en fuentes de TEXTO —
    PyMuPDF recupera prosa legible). Verificado empíricamente: AGaramondPro y
    Times-Bold en Stewart son Type0/Identity sin ToUnicode y emiten español
    perfecto. Por eso exigimos además que lo emitido NO parezca lenguaje
    natural: `alpha_ratio` (fracción de letras sobre imprimible-ASCII) bajo
    ⇒ predominan símbolos/puntuación ⇒ probablemente una fuente-símbolo rota.

    Limitación conocida (Fase 1): una fuente-símbolo que mapee símbolos a
    LETRAS (como MathematicalPi-Five: `=`→`m`) tendría alpha_ratio alto y se
    escaparía de ESTE canal — pero esas las pilla el regex de nombres. El
    hueco real (símbolo→letra Y nombre desconocido) es raro y queda para
    revisión manual del informe.
    """
    if not emits_printable_ascii:
        return False
    if meta.has_tounicode:
        return False
    if not meta.is_identity_type0:
        return False
    return alpha_ratio < _STRUCT_ALPHA_MAX


# ─────────────────────────────────────────────────────────────────────────────
# Agregados
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FontReport:
    """Agregado de una familia de fuente (en un PDF o en todo el corpus)."""
    family: str
    subtype: str = ""
    encoding: str = ""
    has_tounicode: bool = False
    name_pattern_broken: bool = False
    structural_suspect: bool = False
    # codepoint(int) → {"char": str, "count": int, "risk": str}
    codepoints: dict = field(default_factory=dict)
    books: set = field(default_factory=set)

    @property
    def is_suspect(self) -> bool:
        return self.name_pattern_broken or self.structural_suspect

    def _sum(self, risk: str) -> int:
        return sum(v["count"] for v in self.codepoints.values() if v["risk"] == risk)

    @property
    def silent_count(self) -> int:
        return self._sum(SILENT_MISMAP)

    @property
    def control_count(self) -> int:
        return self._sum(MARKERED_CONTROL)

    @property
    def total_glyphs(self) -> int:
        return sum(v["count"] for v in self.codepoints.values())


@dataclass
class CorpusReport:
    fonts: dict = field(default_factory=dict)   # family → FontReport
    pdfs_ok: int = 0
    pdfs_failed: list = field(default_factory=list)  # [(path, error)]

    def blind_spots(self) -> list:
        """Fuentes que corrompen en silencio pero el regex del motor NO detecta."""
        return [
            r for r in self.fonts.values()
            if r.structural_suspect and not r.name_pattern_broken and r.silent_count > 0
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Extracción desde el PDF
# ─────────────────────────────────────────────────────────────────────────────


def _has_tounicode(doc: "fitz.Document", xref: int) -> bool:
    """¿El diccionario de fuente en `xref` declara un /ToUnicode CMap?"""
    try:
        obj = doc.xref_object(xref, compressed=True)
    except Exception:
        return False
    return "/ToUnicode" in (obj or "")


def _font_meta_for_doc(doc: "fitz.Document") -> dict:
    """
    Mapa familia_canónica → FontMeta para todas las fuentes del documento.
    Primer-gana ante múltiples subsets/xrefs de la misma familia (consistente
    con stem_engine: la ambigüedad entre subsets es despreciable en práctica).
    """
    meta: dict = {}
    for pno in range(doc.page_count):
        try:
            fonts = doc[pno].get_fonts(full=True)
        except Exception as exc:
            logger.debug("get_fonts falló en p%d: %s", pno, exc)
            continue
        for f in fonts:
            xref, _ext, subtype, basefont, _refname, encoding = f[0], f[1], f[2], f[3], f[4], f[5]
            fam = canonical_font_key(basefont)
            if not fam or fam in meta:
                continue
            meta[fam] = FontMeta(
                family=fam,
                subtype=subtype or "",
                encoding=encoding or "",
                has_tounicode=_has_tounicode(doc, xref),
                name_pattern_broken=_fr._font_is_broken(fam),
            )
    return meta


def harvest_pdf(path: Path) -> tuple[dict, Optional[str]]:
    """
    Procesa UN pdf. Devuelve (family→FontReport, error_o_None). Robusto:
    cualquier fallo de un PDF se reporta como string, jamás aborta el corpus.
    """
    try:
        doc = fitz.open(path)
    except Exception as exc:
        return {}, f"open: {exc}"
    if doc.needs_pass:
        doc.close()
        return {}, "encrypted (needs password)"

    try:
        meta = _font_meta_for_doc(doc)

        # Paso 1: contar glifos por (familia, codepoint, char), y saber qué
        # familias emiten imprimible-ASCII (necesario para structural_suspect).
        raw_counts: dict = defaultdict(Counter)          # fam → Counter(char)
        emits_printable: dict = defaultdict(bool)        # fam → bool
        print_total: dict = defaultdict(int)             # fam → #imprimible-ASCII
        print_alpha: dict = defaultdict(int)             # fam → #letras imprimibles
        for pno in range(doc.page_count):
            try:
                rd = doc[pno].get_text("rawdict")
            except Exception as exc:
                logger.debug("rawdict falló en %s p%d: %s", path.name, pno, exc)
                continue
            for block in rd.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        fam = canonical_font_key(span.get("font", ""))
                        for chinfo in span.get("chars", []):
                            c = chinfo.get("c")
                            if not c:
                                continue
                            raw_counts[fam][c] += 1
                            if _is_printable_ascii(c):
                                emits_printable[fam] = True
                                print_total[fam] += 1
                                if c.isalpha():
                                    print_alpha[fam] += 1

        # Paso 2: resolver sospecha estructural + clasificar cada codepoint.
        reports: dict = {}
        for fam, charcounts in raw_counts.items():
            # Si por lo que sea no hay metadata estructural de la fuente, al
            # menos el canal del regex de nombres debe seguir vivo (defensa en
            # profundidad: que un fallo de get_fonts no apague TODA la detección).
            m = meta.get(fam) or FontMeta(
                family=fam, name_pattern_broken=_fr._font_is_broken(fam))
            alpha_ratio = (print_alpha[fam] / print_total[fam]
                           if print_total[fam] else 0.0)
            structural = is_structural_suspect(m, emits_printable[fam], alpha_ratio)
            font_suspect = m.name_pattern_broken or structural
            is_grk = _fr._is_grk_font(fam)

            rep = FontReport(
                family=fam,
                subtype=m.subtype,
                encoding=m.encoding,
                has_tounicode=m.has_tounicode,
                name_pattern_broken=m.name_pattern_broken,
                structural_suspect=structural,
                books={path.name},
            )
            for ch, n in charcounts.items():
                cp = ord(ch[0]) if ch else 0
                rep.codepoints[cp] = {
                    "char": ch,
                    "count": n,
                    "risk": glyph_risk(ch, font_suspect, is_grk),
                }
            reports[fam] = rep
        return reports, None
    except Exception as exc:  # nunca dejamos que un PDF tumbe el corpus
        return {}, f"harvest: {exc!r}"
    finally:
        doc.close()


def _merge_into(corpus: CorpusReport, reports: dict) -> None:
    """Funde los reportes de un PDF en el agregado del corpus."""
    for fam, rep in reports.items():
        agg = corpus.fonts.get(fam)
        if agg is None:
            corpus.fonts[fam] = rep
            continue
        # metadata: structural/name flags son OR (si alguna vez fue sospechosa…)
        agg.structural_suspect = agg.structural_suspect or rep.structural_suspect
        agg.name_pattern_broken = agg.name_pattern_broken or rep.name_pattern_broken
        agg.has_tounicode = agg.has_tounicode and rep.has_tounicode
        if not agg.subtype:
            agg.subtype, agg.encoding = rep.subtype, rep.encoding
        agg.books |= rep.books
        for cp, info in rep.codepoints.items():
            cur = agg.codepoints.get(cp)
            if cur is None:
                agg.codepoints[cp] = dict(info)
            else:
                cur["count"] += info["count"]
                # el riesgo es determinista por (fam,char); si difiere, el
                # peor gana (silent > markered > resto) para no subestimar.
                cur["risk"] = _worst_risk(cur["risk"], info["risk"])


_RISK_ORDER = {SILENT_MISMAP: 3, MARKERED_CONTROL: 2, RECOVERED_GRK: 1,
               STRAY_CONTROL: 1, OK: 0}


def _worst_risk(a: str, b: str) -> str:
    return a if _RISK_ORDER.get(a, 0) >= _RISK_ORDER.get(b, 0) else b


def harvest_corpus(paths: Iterable[Path], progress: bool = True) -> CorpusReport:
    """Procesa una lista de PDFs y agrega. Determinista en el orden dado."""
    corpus = CorpusReport()
    paths = list(paths)
    for i, p in enumerate(paths, 1):
        if progress:
            logger.info("[%d/%d] %s", i, len(paths), p.name)
        reports, err = harvest_pdf(p)
        if err is not None:
            corpus.pdfs_failed.append((str(p), err))
            logger.warning("  SKIP %s — %s", p.name, err)
            continue
        corpus.pdfs_ok += 1
        _merge_into(corpus, reports)
    return corpus


# ─────────────────────────────────────────────────────────────────────────────
# Análisis: curva de cobertura Zipf
# ─────────────────────────────────────────────────────────────────────────────


def zipf_coverage(counts: list[int], fractions=(0.50, 0.80, 0.95, 0.99)) -> dict:
    """
    Dada la lista de conteos por fuente (no necesariamente ordenada), devuelve
    {fracción: nº de fuentes (las más grandes) necesarias para cubrirla}.
    El número que decide si curar el diccionario es viable.
    """
    ordered = sorted(counts, reverse=True)
    total = sum(ordered)
    out = {f: 0 for f in fractions}
    if total == 0:
        return out
    acc = 0
    for i, c in enumerate(ordered, 1):
        acc += c
        for f in fractions:
            if out[f] == 0 and acc / total >= f:
                out[f] = i
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Reporte
# ─────────────────────────────────────────────────────────────────────────────


def render_report(corpus: CorpusReport, top: int = 25) -> str:
    L: list[str] = []
    suspects = [r for r in corpus.fonts.values() if r.is_suspect]
    total_silent = sum(r.silent_count for r in corpus.fonts.values())
    total_control = sum(r.control_count for r in corpus.fonts.values())

    L.append("=" * 78)
    L.append("FONT HARVESTER — inventario de fuentes rotas")
    L.append("=" * 78)
    L.append(f"PDFs procesados OK : {corpus.pdfs_ok}")
    L.append(f"PDFs descartados   : {len(corpus.pdfs_failed)}")
    L.append(f"Familias de fuente : {len(corpus.fonts)}  ({len(suspects)} sospechosas)")
    L.append(f"Glifos SILENCIOSOS : {total_silent:,}   (corrupción que engaña)")
    L.append(f"Glifos control     : {total_control:,}   (marcados/recuperables)")
    L.append("")

    # Curva Zipf sobre glifos silenciosos
    zc = zipf_coverage([r.silent_count for r in suspects])
    L.append("COBERTURA ZIPF de glifos silenciosos (nº de fuentes para cubrir X%):")
    for frac, n in zc.items():
        L.append(f"   {int(frac*100):3d}%  →  {n} fuentes")
    L.append("")

    # Ranking de fuentes sospechosas por glifos silenciosos
    L.append(f"TOP {top} FUENTES por glifos silenciosos:")
    L.append(f"   {'familia':32s} {'subtype':9s} {'enc':11s} {'2Uni':4s} "
             f"{'silent':>8s} {'ctrl':>7s} {'name?':5s} {'struct?':7s}")
    ranked = sorted(corpus.fonts.values(), key=lambda r: r.silent_count, reverse=True)
    for r in ranked[:top]:
        if r.silent_count == 0 and r.control_count == 0:
            continue
        L.append(f"   {r.family[:32]:32s} {r.subtype[:9]:9s} {r.encoding[:11]:11s} "
                 f"{'yes' if r.has_tounicode else 'NO ':4s} "
                 f"{r.silent_count:8,d} {r.control_count:7,d} "
                 f"{'Y' if r.name_pattern_broken else '.':5s} "
                 f"{'Y' if r.structural_suspect else '.':7s}")
    L.append("")

    # Puntos ciegos: el motor ni los detecta
    blind = sorted(corpus.blind_spots(), key=lambda r: r.silent_count, reverse=True)
    L.append(f"PUNTOS CIEGOS DEL MOTOR ({len(blind)} fuentes — structural pero el "
             f"regex NO las detecta):")
    if not blind:
        L.append("   (ninguno)")
    for r in blind[:top]:
        L.append(f"   {r.family[:40]:40s} silent={r.silent_count:,}  "
                 f"{r.subtype}/{r.encoding}")
    L.append("")

    if corpus.pdfs_failed:
        L.append("PDFs DESCARTADOS:")
        for p, e in corpus.pdfs_failed:
            L.append(f"   {Path(p).name}: {e}")
    L.append("=" * 78)
    return "\n".join(L)


def build_inventory(corpus: CorpusReport, min_count: int = 1) -> dict:
    """
    Worklist de curación (JSON-serializable) para la Fase 2. Solo fuentes
    sospechosas; por cada una, sus codepoints silenciosos/control ordenados
    por frecuencia, con el char emitido y un campo `unicode` vacío a rellenar.
    """
    out: dict = {"summary": {}, "fonts": []}
    suspects = [r for r in corpus.fonts.values() if r.is_suspect]
    out["summary"] = {
        "pdfs_ok": corpus.pdfs_ok,
        "pdfs_failed": len(corpus.pdfs_failed),
        "fonts_total": len(corpus.fonts),
        "fonts_suspect": len(suspects),
        "silent_total": sum(r.silent_count for r in corpus.fonts.values()),
        "zipf_silent": {str(k): v for k, v in
                        zipf_coverage([r.silent_count for r in suspects]).items()},
        "blind_spots": [r.family for r in corpus.blind_spots()],
    }
    for r in sorted(suspects, key=lambda r: r.silent_count, reverse=True):
        entries = []
        for cp, info in sorted(r.codepoints.items(),
                               key=lambda kv: kv[1]["count"], reverse=True):
            if info["risk"] not in (SILENT_MISMAP, MARKERED_CONTROL):
                continue
            if info["count"] < min_count:
                continue
            entries.append({
                "codepoint": f"U+{cp:04X}",
                "emitted_char": info["char"],
                "count": info["count"],
                "risk": info["risk"],
                "unicode": "",   # ← a rellenar en curación (Fase 2)
            })
        if not entries:
            continue
        out["fonts"].append({
            "family": r.family,
            "subtype": r.subtype,
            "encoding": r.encoding,
            "has_tounicode": r.has_tounicode,
            "name_pattern_broken": r.name_pattern_broken,
            "structural_suspect": r.structural_suspect,
            "books": sorted(r.books),
            "silent_count": r.silent_count,
            "glyphs": entries,
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def _collect_pdfs(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    return sorted(target.rglob("*.pdf"))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Inventario de fuentes rotas (Fase 1).")
    ap.add_argument("target", type=Path, help="PDF o carpeta (recursivo) de PDFs")
    ap.add_argument("--json-out", type=Path, help="Inventario/worklist JSON")
    ap.add_argument("--report-out", type=Path, help="Informe humano (txt)")
    ap.add_argument("--min-count", type=int, default=1,
                    help="Frecuencia mínima de un glifo para entrar al inventario")
    ap.add_argument("--top", type=int, default=25, help="Filas en el ranking")
    ap.add_argument("-q", "--quiet", action="store_true", help="Menos progreso")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
    )

    pdfs = _collect_pdfs(args.target)
    if not pdfs:
        logger.error("No se encontraron PDFs en %s", args.target)
        return 2

    corpus = harvest_corpus(pdfs, progress=not args.quiet)
    report = render_report(corpus, top=args.top)
    print(report)

    if args.report_out:
        args.report_out.write_text(report, encoding="utf-8")
        logger.info("Informe escrito en %s", args.report_out)
    if args.json_out:
        inv = build_inventory(corpus, min_count=args.min_count)
        args.json_out.write_text(json.dumps(inv, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        logger.info("Inventario escrito en %s", args.json_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
