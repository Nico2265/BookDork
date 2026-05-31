"""
=============================================================================
bench_glyph_name_recovery.py — Banco de medición y harvesting del canal de
                               recuperación por NOMBRE de glifo.
=============================================================================
Cuantifica, sobre un PDF real, cuántos glifos de fuentes simples se recuperan
de forma DETERMINISTA por nombre (antes de invocar el matcher visual), y vuelca
los nombres no resueltos para construir el suplemento empíricamente.

USO
───
    set PYTHONIOENCODING=utf-8

    # Sobre un PDF real (Zill, Anton, etc.):
    python tests/bench_glyph_name_recovery.py "ruta/al/Zill.pdf"
    python tests/bench_glyph_name_recovery.py "ruta/al/Zill.pdf" --harvest
    python tests/bench_glyph_name_recovery.py "ruta/al/Zill.pdf" --pages 40

    # Sin argumento: corre una DEMOSTRACIÓN sobre un PDF sintético que
    # reproduce el modo de fallo MathematicalPi (no sustituye a un PDF real).
    python tests/bench_glyph_name_recovery.py

También respeta la variable de entorno BOOKDORK_BENCH_PDF como ruta por defecto.

QUÉ MIDE
────────
  * occurrences      : nº de glifos emitidos por cada fuente candidata
  * resolved_by_name : recuperados deterministamente (desglose AGL/suplemento)
  * abstained        : sin nombre declarado / con nombre desconocido
  * unresolved names : tabla {nombre: frecuencia, fuente} → insumo del suplemento

Es independiente de font_recovery (visual): mide el canal ortogonal.
=============================================================================
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(_BACKEND))

import fitz  # noqa: E402
import glyph_name_recovery as gnr  # noqa: E402
from font_recovery import _font_is_broken  # noqa: E402

_SIMPLE_SUBTYPES = {"/Type1", "/TrueType", "/MMType1", "/Type3"}


def _is_broken_glyph(c: str) -> bool:
    """
    ¿El carácter emitido salió ROTO (código crudo)? Es el denominador honesto:
    el canal de nombres sólo tiene sentido sobre lo que MuPDF NO supo resolver.
    Cubre control C0 (sin whitespace), DEL, control C1, y Private-Use Area.
    NOTA: el bug de truncamiento uXXXXXX NO cae aquí (MuPDF emite un carácter
    imprimible erróneo, no un código crudo); detectarlo requiere integración a
    nivel de glyph-id y queda fuera de esta medición.
    """
    if not c:
        return False
    o = ord(c)
    if o < 0x20:
        return not c.isspace()
    if o == 0x7F or 0x80 <= o <= 0x9F:
        return True
    return (0xE000 <= o <= 0xF8FF or 0xF0000 <= o <= 0xFFFFD or 0x100000 <= o <= 0x10FFFD)


def _build_synthetic_pdf(path: Path) -> None:
    """PDF sintético que reproduce el fallo MathematicalPi (3 códigos)."""
    doc = fitz.open()
    page = doc.new_page(width=300, height=200)
    enc = doc.get_new_xref()
    doc.update_object(enc, "<< /Type /Encoding /Differences "
                           "[ 2 /summation 3 /u1D6FC 4 /H11001 5 /integral 6 /H11002 ] >>")
    fx = doc.get_new_xref()
    doc.update_object(fx, f"<< /Type /Font /Subtype /Type1 "
                          f"/BaseFont /ABCDEF+MathematicalPi-One /Encoding {enc} 0 R >>")
    cx = doc.get_new_xref()
    doc.update_object(cx, "<< >>")
    doc.update_stream(cx, b"BT /F1 24 Tf 40 100 Td (\\002\\003\\004\\005\\006\\004) Tj ET")
    doc.xref_set_key(page.xref, "Contents", f"{cx} 0 R")
    doc.xref_set_key(page.xref, "Resources", f"<< /Font << /F1 {fx} 0 R >> >>")
    doc.save(str(path))
    doc.close()


def _strip_subset(name: str) -> str:
    """Quita el prefijo de subset 'ABCDEF+' (get_fonts lo trae; el span no)."""
    return (name or "").split("+", 1)[-1]


def _font_name_to_xref(doc, page_indices) -> dict[str, int]:
    """Mapa basefont(sin subset)→xref (último gana ante colisiones)."""
    name2xref: dict[str, int] = {}
    for i in page_indices:
        for f in doc[i].get_fonts(full=True):
            xref, basefont = f[0], f[3]
            name2xref[_strip_subset(basefont)] = xref
    return name2xref


def run(doc, max_pages: int | None) -> dict:
    """Recorre el documento y mide la recuperación por nombre."""
    n_total = len(doc)
    page_indices = list(range(n_total if max_pages is None else min(max_pages, n_total)))

    name2xref = _font_name_to_xref(doc, page_indices)
    rec = gnr.GlyphNameRecovery(doc, supplement=gnr.load_supplement())

    # occurrences[font_name][code] = nº de apariciones
    occ: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for i in page_indices:
        raw = doc[i].get_text("rawdict")
        for block in raw.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    font = span.get("font", "")
                    for ch in span.get("chars", []):
                        c = ch.get("c")
                        # Sólo glifos ROTOS: ahí ord(c) == código de glifo y el
                        # canal de nombres es relevante (MuPDF no supo resolver).
                        if c and _is_broken_glyph(c):
                            occ[font][ord(c)] += 1

    # Resuelve por (font, code) una sola vez y multiplica por la frecuencia.
    per_font: dict[str, dict] = {}
    unresolved: dict[tuple[str, str], int] = defaultdict(int)  # (font, name)→freq
    for font, codes in occ.items():
        xref = name2xref.get(_strip_subset(font))
        if xref is None:
            continue
        _, subtype = rec._xref_get(xref, "Subtype")
        diag = rec.font_diagnostics(xref)
        total = sum(codes.values())
        res_agl = res_sup = abst_noname = abst_unknown = 0
        for code, freq in codes.items():
            r = rec.resolve(xref, code)
            if r is None:
                name = diag.code_to_name.get(code)
                if name:
                    abst_unknown += freq
                    unresolved[(font, name)] += freq
                else:
                    abst_noname += freq
            elif r.source == gnr.SOURCE_AGL:
                res_agl += freq
            else:
                res_sup += freq
        per_font[font] = {
            "xref": xref, "subtype": subtype, "broken": _font_is_broken(font),
            "has_tounicode": diag.has_tounicode, "name_source": diag.source,
            "occurrences": total, "resolved_agl": res_agl, "resolved_supplement": res_sup,
            "abstained_no_name": abst_noname, "abstained_unknown": abst_unknown,
            "distinct_codes": len(codes),
        }

    return {"pages_scanned": len(page_indices), "per_font": per_font,
            "unresolved": dict(unresolved), "global_stats": rec.stats()}


def print_report(pdf_path: str, report: dict, harvest: bool) -> None:
    pf = report["per_font"]
    print(f"\n=== Glyph-Name Recovery Benchmark ===")
    print(f"PDF: {pdf_path}")
    print(f"Páginas escaneadas: {report['pages_scanned']}\n")

    if not pf:
        print("  (ninguna fuente emitió glifos ROTOS — nada que recuperar por este canal;\n"
              "   MuPDF resolvió todo, o el PDF no tiene fuentes con CMap rota)\n")
        return

    print("Denominador = glifos ROTOS (código crudo). Mide cuánto recupera el "
          "canal de nombres\nde lo que MuPDF NO supo resolver.\n")
    # Foco en fuentes ROTAS (las que el canal busca recuperar) + resto simple.
    print(f"{'fuente':38} {'roto':5} {'toUni':6} {'fuente-nombres':14} {'rotos':8} "
          f"{'AGL':7} {'supl':6} {'sin-nom':8} {'descon':7} {'recup%':7}")
    print("-" * 120)
    grand = defaultdict(int)
    for font, d in sorted(pf.items(), key=lambda kv: -kv[1]["occurrences"]):
        recovered = d["resolved_agl"] + d["resolved_supplement"]
        pct = (100.0 * recovered / d["occurrences"]) if d["occurrences"] else 0.0
        print(f"{font[:38]:38} {str(d['broken']):5} {str(d['has_tounicode']):6} "
              f"{d['name_source'] or '—':14} {d['occurrences']:8} "
              f"{d['resolved_agl']:7} {d['resolved_supplement']:6} "
              f"{d['abstained_no_name']:8} {d['abstained_unknown']:7} {pct:6.1f}%")
        for k in ("occurrences", "resolved_agl", "resolved_supplement",
                  "abstained_no_name", "abstained_unknown"):
            grand[k] += d[k]

    tot = grand["occurrences"]
    rec_tot = grand["resolved_agl"] + grand["resolved_supplement"]
    print("-" * 120)
    print(f"TOTAL glifos={tot}  recuperados-por-nombre={rec_tot} "
          f"({100.0*rec_tot/tot if tot else 0:.1f}%)  "
          f"[AGL={grand['resolved_agl']} supl={grand['resolved_supplement']}]  "
          f"abstención: sin-nombre={grand['abstained_no_name']} "
          f"desconocido={grand['abstained_unknown']}")

    # Tabla de cosecha: nombres no resueltos → construir suplemento.
    unres = report["unresolved"]
    if unres:
        print(f"\n--- Nombres NO resueltos (candidatos a suplemento, "
              f"verificar visualmente antes de añadir) ---")
        print(f"{'frecuencia':10} {'nombre':24} {'fuente'}")
        for (font, name), freq in sorted(unres.items(), key=lambda kv: -kv[1]):
            print(f"{freq:10} {name[:24]:24} {font}")
        if harvest:
            out = Path(__file__).with_name("harvest_unresolved.json")
            import json
            payload = {"_note": "Nombres no resueltos por glyph_name_recovery. "
                                 "Verifica cada glifo visualmente y mueve las "
                                 "entradas confirmadas a glyph_name_supplement.json",
                       "by_name": {name: {"freq": freq, "font": font}
                                   for (font, name), freq in unres.items()}}
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n[harvest] volcado a {out}")
    else:
        print("\n(Sin nombres no resueltos: todo lo nombrado se resolvió por AGL/suplemento.)")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="Banco del canal de recuperación por nombre de glifo.")
    ap.add_argument("pdf", nargs="?", default=os.environ.get("BOOKDORK_BENCH_PDF"),
                    help="Ruta al PDF. Si se omite, corre una demostración sintética.")
    ap.add_argument("--pages", type=int, default=None, help="Limitar nº de páginas a escanear.")
    ap.add_argument("--harvest", action="store_true",
                    help="Volcar nombres no resueltos a harvest_unresolved.json.")
    args = ap.parse_args()

    synthetic = False
    pdf_path = args.pdf
    if not pdf_path:
        synthetic = True
        pdf_path = str(Path(__file__).with_name("_bench_synthetic.pdf"))
        _build_synthetic_pdf(Path(pdf_path))
        print("\n[!] Sin PDF de entrada: ejecutando DEMOSTRACIÓN sintética "
              "(reproduce el fallo MathematicalPi, NO sustituye a un PDF real).")
    elif not Path(pdf_path).exists():
        print(f"ERROR: no existe el PDF: {pdf_path}", file=sys.stderr)
        return 2

    doc = fitz.open(pdf_path)
    try:
        report = run(doc, args.pages)
    finally:
        doc.close()

    print_report(pdf_path, report, args.harvest)

    if synthetic:
        try:
            Path(pdf_path).unlink()
        except OSError:
            pass
        print("[i] Para medir recuperación real, pásame el PDF de Zill:\n"
              '    python tests/bench_glyph_name_recovery.py "ruta/al/Zill.pdf" --harvest\n')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
