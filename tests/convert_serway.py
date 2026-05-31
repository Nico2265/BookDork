"""Convierte Serway-7Ed.pdf a Markdown con el motor del proyecto y mide fidelidad."""
from __future__ import annotations
import importlib.util, json, os, sys, time
from pathlib import Path

ROOT = Path(__file__).parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND.resolve()))

PDF = Path(r"C:\Users\nicof\Downloads\Serway-7Ed.pdf")
OUT_DIR = ROOT / "tests" / "serway_out"
OUT_DIR.mkdir(exist_ok=True)

os.environ["BOOKDORK_STEM_ENGINE"] = "1"
for m in ("stem_engine", "stem_validator", "fidelity_metrics", "pdf_engine"):
    spec = importlib.util.spec_from_file_location(m, BACKEND / f"{m}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[m] = mod
    spec.loader.exec_module(mod)
import fitz  # noqa: E402

pdf_engine = sys.modules["pdf_engine"]
stem_engine = sys.modules["stem_engine"]
fidelity_metrics = sys.modules["fidelity_metrics"]
stem_validator = sys.modules["stem_validator"]

content = PDF.read_bytes()
doc = fitz.open(stream=content, filetype="pdf")
density = stem_engine.score_stem_density(doc)
n_pages = doc.page_count
doc.close()
print(f"[serway] {n_pages} páginas · STEM density={density:.4f} (threshold 0.05)", flush=True)

t0 = time.perf_counter()
res = pdf_engine.convert_document(content, ".pdf", "Serway-7Ed.pdf")
elapsed = time.perf_counter() - t0

md = res.get("markdown", "")
(OUT_DIR / "Serway-7Ed.md").write_text(md, encoding="utf-8")

val = stem_validator.validate_markdown(md)
rep = fidelity_metrics.compute(content, md, equation_validity=val.validity_rate)

summary = {
    "pages": n_pages,
    "stem_density": density,
    "engine_used": res.get("engine_used"),
    "success": res.get("success"),
    "elapsed_s": round(elapsed, 1),
    "char_count": res.get("char_count"),
    "word_count": res.get("word_count"),
    "fidelity": rep.to_dict(),
    "validation": {
        "total_equations": val.total_equations,
        "valid_equations": val.valid_equations,
        "validity_rate": val.validity_rate,
        "unbalanced_braces": val.unbalanced_braces,
        "orphan_markers": val.orphan_markers,
        "latex_parse_errors": val.latex_parse_errors,
    },
}
(OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"[serway] engine={res.get('engine_used')} elapsed={elapsed:.1f}s chars={res.get('char_count')} words={res.get('word_count')}", flush=True)
print(f"[serway] fidelity.overall={rep.overall*100:.2f}% glyph={rep.glyph_coverage*100:.1f}% moji={rep.mojibake_score*100:.1f}% sym={rep.symbol_preservation*100:.1f}% super_sub={rep.super_sub_detection*100:.1f}%", flush=True)
print(f"[serway] equations valid {val.valid_equations}/{val.total_equations}", flush=True)
print("[serway] DONE -> tests/serway_out/Serway-7Ed.md", flush=True)
