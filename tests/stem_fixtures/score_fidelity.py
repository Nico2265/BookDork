"""
Calcula métricas de fidelidad de un Markdown contra el ground truth del PDF.

Métricas:
  - words_preserved      : palabras del ground truth presentes (sin ligaduras rotas)
  - symbols_preserved    : símbolos Unicode math/Greek presentes
  - mojibake_free        : 1.0 si no hay ﬁ/ﬀ/secuencias rotas
  - super_sub_preserved  : ¿hay ^ o _ donde debería haber super/subscripts?
  - structural_score     : ¿hay headings detectados?
  - overall              : promedio ponderado (target ≥ 0.99)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def score(md_path: Path, gt_path: Path) -> dict:
    md = md_path.read_text(encoding="utf-8")
    gt = json.loads(gt_path.read_text(encoding="utf-8"))

    # 1. Palabras que DEBEN aparecer
    words_total = len(gt["must_contain_words"])
    words_ok    = sum(1 for w in gt["must_contain_words"] if w in md)
    words_score = words_ok / words_total if words_total else 1.0

    # 2. Símbolos Unicode preservados
    sym_total = len(gt["must_contain_symbols"])
    sym_ok    = sum(1 for s in gt["must_contain_symbols"] if s in md)
    sym_score = sym_ok / sym_total if sym_total else 1.0

    # 3. Mojibake / ligaduras / fragmentos prohibidos
    forbidden_hits = sum(1 for f in gt["must_not_contain"] if f in md)
    mojibake_score = 1.0 if forbidden_hits == 0 else max(0.0, 1.0 - forbidden_hits / len(gt["must_not_contain"]))

    # 4. Super/subscripts: ¿se preservó el marcador semántico?
    sem_total = len(gt["semantic_checks"])
    sem_ok = 0
    for check in gt["semantic_checks"]:
        # Busca el contexto seguido (cercanamente) del marcador esperado
        # Tolera espacios y caracteres LaTeX como {
        ctx = re.escape(check["context"])
        marker = re.escape(check["must_be_followed_by"])
        # El marcador debe aparecer en los siguientes 3 chars después del contexto
        pat = ctx + r"[\s{]*" + marker
        if re.search(pat, md):
            sem_ok += 1
    sem_score = sem_ok / sem_total if sem_total else 1.0

    # 5. Estructura: headings detectados (linea que empieza con #)
    headings_found = [
        ln.strip() for ln in md.splitlines()
        if ln.strip().startswith("#")
    ]
    # O headings detectados por coincidencia textual con el GT
    gt_headings = gt["headings"]
    h_textual_hits = sum(1 for h in gt_headings if h in md)
    # Score combinado: usa cualquiera que sea mayor
    h_markdown_score = min(1.0, len(headings_found) / max(1, len(gt_headings)))
    h_textual_score  = h_textual_hits / max(1, len(gt_headings))
    structural_score = max(h_markdown_score, h_textual_score)

    # 6. Equation containment: ¿están las ecuaciones representadas?
    # Soporta equivalencia LaTeX↔Unicode (e.g., \hbar ↔ ℏ).
    LATEX_TO_UNICODE = {
        "hbar":   "ℏ",   "infty":  "∞",   "partial": "∂",   "nabla": "∇",
        "int":    "∫",   "sum":    "∑",   "prod":    "∏",
        "alpha":  "α",   "beta":   "β",   "gamma":   "γ",   "delta": "δ",
        "epsilon":"ε",   "zeta":   "ζ",   "eta":     "η",   "theta": "θ",
        "iota":   "ι",   "kappa":  "κ",   "lambda":  "λ",   "mu":    "μ",
        "nu":     "ν",   "xi":     "ξ",   "pi":      "π",   "rho":   "ρ",
        "sigma":  "σ",   "tau":    "τ",   "phi":     "φ",   "chi":   "χ",
        "psi":    "ψ",   "omega":  "ω",
        "Delta":  "Δ",   "Gamma":  "Γ",   "Sigma":   "Σ",   "Omega": "Ω",
        "hat":    "̂",    # combinante (Ĥ contiene U+0302)
        "dx":     "dx",  "dy": "dy",  # diferenciales — preserve as-is
    }
    eq_components = 0
    eq_total = 0
    for eq in gt["equations_latex"]:
        tokens = re.findall(r"[A-Za-z]+|\d+", eq)
        for tok in tokens:
            eq_total += 1
            if tok in md:
                eq_components += 1
                continue
            # Equivalencia Unicode
            uni = LATEX_TO_UNICODE.get(tok)
            if uni and uni in md:
                eq_components += 1
                continue
            # Caso "hat": Ĥ tiene U+0302 (combining) o H con ̂; aceptar Ĥ
            if tok == "hat" and "Ĥ" in md:
                eq_components += 1
    eq_score = eq_components / eq_total if eq_total else 1.0

    # ── Promedio ponderado ────────────────────────────────────────────────────
    weights = {
        "text_words":         0.15,
        "math_symbols":       0.20,
        "mojibake_free":      0.15,
        "super_sub":          0.30,   # peso alto: es EL problema crítico
        "structure":          0.10,
        "equation_tokens":    0.10,
    }
    scores = {
        "text_words":      words_score,
        "math_symbols":    sym_score,
        "mojibake_free":   mojibake_score,
        "super_sub":       sem_score,
        "structure":       structural_score,
        "equation_tokens": eq_score,
    }
    overall = sum(scores[k] * w for k, w in weights.items())

    return {
        "scores": scores,
        "weights": weights,
        "overall": overall,
        "details": {
            "words_ok": f"{words_ok}/{words_total}",
            "symbols_ok": f"{sym_ok}/{sym_total}",
            "forbidden_hits": forbidden_hits,
            "sem_ok": f"{sem_ok}/{sem_total}",
            "headings_md": len(headings_found),
            "headings_textual": f"{h_textual_hits}/{len(gt_headings)}",
            "eq_token_coverage": f"{eq_components}/{eq_total}",
        },
        "missing_symbols": [s for s in gt["must_contain_symbols"] if s not in md],
        "found_forbidden": [f for f in gt["must_not_contain"] if f in md],
    }


def print_report(label: str, result: dict) -> None:
    print(f"\n{'═' * 70}")
    print(f"  {label}")
    print(f"{'═' * 70}")
    print(f"\n  OVERALL FIDELITY: {result['overall']*100:.2f}%")
    print(f"\n  Breakdown:")
    for k, v in result["scores"].items():
        w = result["weights"][k]
        bar = "█" * int(v * 20) + "░" * (20 - int(v * 20))
        print(f"    {k:18s}  {v*100:6.2f}%  weight={w:.2f}  {bar}")
    print(f"\n  Details:")
    for k, v in result["details"].items():
        print(f"    {k:22s} = {v}")
    if result["missing_symbols"]:
        print(f"\n  ❌ Símbolos perdidos: {result['missing_symbols']}")
    if result["found_forbidden"]:
        print(f"  ❌ Fragmentos prohibidos hallados: {result['found_forbidden']}")


if __name__ == "__main__":
    HERE = Path(__file__).parent
    md_path = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "baseline_output.md"
    gt_path = HERE / "ground_truth.json"
    label = sys.argv[2] if len(sys.argv) > 2 else md_path.stem.upper()
    result = score(md_path, gt_path)
    print_report(label, result)

    # JSON output para automatización
    out_json = md_path.with_suffix(".score.json")
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  JSON: {out_json}")
