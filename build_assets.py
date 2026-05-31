"""
build_assets.py — Pipeline de minificación + hash fingerprinting.

Lee Frontend/, produce Frontend/dist/ con:
  - JS y CSS minificados, renombrados a `<name>.<hash8>.js|.css`
  - Imports ES y referencias HTML reescritos al nombre hasheado
  - Strings legacy de cache busting (?v=N) eliminados
  - manifest.json con el mapeo original -> hasheado (no servido al cliente)

Pensado para correr en build/deploy. El servidor sirve Frontend/dist/ si existe
(ver main.py); en su ausencia sirve Frontend/ fuente para flujo de desarrollo.

Uso:
    python build_assets.py
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

try:
    import jsmin
    import rcssmin
except ImportError as exc:
    sys.exit(f"Falta dependencia: {exc.name}. Instala con: pip install jsmin rcssmin")

ROOT = Path(__file__).parent
SRC  = ROOT / "Frontend"
DST  = SRC / "dist"

# JS sin imports de /static/* (hojas del grafo de dependencias). Se procesan
# primero para que sus importadores puedan reescribir las rutas con el hash.
_LEAF_JS = {"firebase.js", "ui.js"}

# Patrón único para imports estáticos y dinámicos de módulos locales:
#   import ... from '/static/foo.js'
#   import('/static/foo.js')
_IMPORT_RX = re.compile(r"""(['"])/static/([\w\-/.]+\.js)\1""")

# Patrón para referencias HTML a /static/*.{js,css}, opcionalmente con ?v=N.
_HTML_REF_RX = re.compile(r"""(['"])/static/([\w\-/.]+\.(?:js|css))(\?[^'"]*)?\1""")

# Template literals ES6 con backticks. jsmin no los reconoce y trata `//` interno
# como comentario de línea, lo que corrompe URLs como `https://...`. Se neutraliza
# escapando las barras (`/` -> `\/`) — semánticamente idéntico en strings JS.
# El patrón aproxima un template literal (sin manejar anidación profunda con `${`),
# suficiente para los casos del proyecto. flags=DOTALL para multilínea.
_TEMPLATE_LITERAL_RX = re.compile(r"`(?:[^`\\]|\\.)*`", re.DOTALL)


def _neutralize_template_literal_slashes(source: str) -> str:
    """Sustituye `//` por `\\/\\/` dentro de template literals ES6 (backtick).

    jsmin trata `//` como inicio de comentario de línea incluso dentro de
    template literals, lo que rompe URLs y otros patrones. Escapar la barra
    como `\\/` es válido en JavaScript (escape redundante) y produce el mismo
    caracter en runtime, pero evita que jsmin lo confunda con un comentario.
    """

    def _repl(m: re.Match) -> str:
        return m.group(0).replace("//", r"\/\/")

    return _TEMPLATE_LITERAL_RX.sub(_repl, source)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:8]


def _hashed_name(orig: str, content: bytes) -> str:
    stem, _, ext = orig.rpartition(".")
    return f"{stem}.{_hash(content)}.{ext}"


def _ensure_clean_dist() -> None:
    if DST.exists():
        shutil.rmtree(DST)
    DST.mkdir(parents=True)


def _rewrite_js_imports(source: str, js_map: dict[str, str]) -> str:
    """Sustituye '/static/foo.js' por '/static/foo.<hash>.js' usando js_map."""

    def _repl(m: re.Match) -> str:
        quote, name = m.group(1), m.group(2)
        new_name = js_map.get(name)
        if not new_name:
            # Import a un módulo desconocido: dejar tal cual (defensivo).
            return m.group(0)
        return f"{quote}/static/{new_name}{quote}"

    return _IMPORT_RX.sub(_repl, source)


def _rewrite_html(source: str, asset_map: dict[str, str]) -> str:
    """Sustituye refs /static/foo.css|.js (y elimina ?v=N) usando asset_map."""

    def _repl(m: re.Match) -> str:
        quote, name = m.group(1), m.group(2)
        new_name = asset_map.get(name)
        if not new_name:
            return m.group(0)
        return f"{quote}/static/{new_name}{quote}"

    return _HTML_REF_RX.sub(_repl, source)


def _write(name: str, data: bytes) -> Path:
    path = DST / name
    path.write_bytes(data)
    return path


def build() -> dict[str, str]:
    if not SRC.exists():
        sys.exit(f"No existe el directorio fuente: {SRC}")

    _ensure_clean_dist()

    manifest: dict[str, str] = {}

    # ── 1. CSS — sin dependencias internas ────────────────────────────────────
    for css in sorted(SRC.glob("*.css")):
        minified = rcssmin.cssmin(css.read_text(encoding="utf-8")).encode("utf-8")
        hashed   = _hashed_name(css.name, minified)
        _write(hashed, minified)
        manifest[css.name] = hashed
        print(f"  CSS  {css.name:24s} -> {hashed}")

    # ── 2. JS hoja — sin imports de /static/* ─────────────────────────────────
    for name in sorted(_LEAF_JS):
        src = SRC / name
        if not src.exists():
            continue
        source   = _neutralize_template_literal_slashes(src.read_text(encoding="utf-8"))
        minified = jsmin.jsmin(source).encode("utf-8")
        hashed   = _hashed_name(name, minified)
        _write(hashed, minified)
        manifest[name] = hashed
        print(f"  JS#1 {name:24s} -> {hashed}")

    # ── 3. JS importador — reescribir imports con el hash de las hojas ───────
    js_map = {k: v for k, v in manifest.items() if k.endswith(".js")}
    for js in sorted(SRC.glob("*.js")):
        if js.name in _LEAF_JS:
            continue
        rewritten = _rewrite_js_imports(js.read_text(encoding="utf-8"), js_map)
        prepared  = _neutralize_template_literal_slashes(rewritten)
        minified  = jsmin.jsmin(prepared).encode("utf-8")
        hashed    = _hashed_name(js.name, minified)
        _write(hashed, minified)
        manifest[js.name] = hashed
        print(f"  JS#2 {js.name:24s} -> {hashed}")

    # ── 4. HTML — reescribir todas las refs a assets hasheados ───────────────
    for html in sorted(SRC.glob("*.html")):
        rewritten = _rewrite_html(html.read_text(encoding="utf-8"), manifest)
        (DST / html.name).write_text(rewritten, encoding="utf-8")
        print(f"  HTML {html.name}")

    # ── 5. Copias 1:1 — assets binarios sin transformación ───────────────────
    for binary in ("logo.png",):
        src_bin = SRC / binary
        if src_bin.exists():
            shutil.copy2(src_bin, DST / binary)
            print(f"  COPY {binary}")

    # ── 6. Manifest — debugging/auditoría, no servido al cliente ─────────────
    (DST / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )

    return manifest


def main() -> int:
    print(f"Building assets {SRC} -> {DST}")
    manifest = build()
    print(f"\nBuild OK: {len(manifest)} archivos hasheados")
    return 0


if __name__ == "__main__":
    sys.exit(main())
