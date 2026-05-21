"""
start.py — Arranque orquestado de BookDork
==========================================
Fases:
  1. Carga configuracion desde .env
  2. Verifica Docker y garantiza que Meilisearch corre con la clave correcta
  3. Health check: espera a que Meilisearch responda antes de continuar
  4. Arranca el servidor FastAPI

Modos:
  python start.py        -> produccion: Hypercorn con HTTPS + HTTP/3 (si hay certs)
  python start.py --dev  -> desarrollo: uvicorn con --reload (HTTP plano)

HTTP/3 requiere certificado TLS generado con:
    python gen_certs.py
"""

import asyncio
import subprocess
import sys
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

# ── Salida ────────────────────────────────────────────────────────────────────
_OK   = "[ OK ]"
_WARN = "[WARN]"
_ERR  = "[ !! ]"
_INFO = "[ .. ]"

def _line(tag: str, msg: str) -> None:
    print(f"  {tag}  {msg}")

def _header(title: str) -> None:
    bar = "=" * 60
    print(f"\n{bar}\n  {title}\n{bar}")

def _ok(msg):   _line(_OK,   msg)
def _warn(msg): _line(_WARN, msg)
def _err(msg):  _line(_ERR,  msg)
def _info(msg): _line(_INFO, msg)


# ── Alias de paquete: "Pyton Backend" -> importable como "backend" ────────────
ROOT        = Path(__file__).parent
BACKEND_DIR = ROOT / "Pyton Backend"

_pkg             = types.ModuleType("backend")
_pkg.__path__    = [str(BACKEND_DIR)]
_pkg.__package__ = "backend"
sys.modules["backend"] = _pkg


# ─────────────────────────────────────────────────────────────────────────────
# FASE 1 — Configuracion
# ─────────────────────────────────────────────────────────────────────────────

def _parse_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        result[k.strip()] = v.strip().strip('"').strip("'")
    return result


ENV = _parse_env(ROOT / ".env")

_MEILI_HOST  = ENV.get("MEILI_HOST",       "http://localhost:7700")
_MEILI_KEY   = ENV.get("MEILI_MASTER_KEY", "changeme_strong_master_key")
_MEILI_PORT  = 7700
_CONTAINER   = "meilisearch"
_MEILI_IMAGE = "getmeili/meilisearch:latest"

# --dev fuerza uvicorn; sin el flag -> Hypercorn (HTTP/3)
# DEBUG en .env controla verbosidad de logs, no el servidor
_DEV_MODE = "--dev" in sys.argv


# ─────────────────────────────────────────────────────────────────────────────
# FASE 2 — Docker / Meilisearch
# ─────────────────────────────────────────────────────────────────────────────

def _run(*cmd: str) -> tuple[int, str]:
    r = subprocess.run(list(cmd), capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout + r.stderr).strip()


def _docker_ok() -> bool:
    code, out = _run("docker", "info")
    if code != 0:
        if "pipe" in out.lower() or "cannot connect" in out.lower():
            _err("Docker Desktop no esta corriendo.")
            _info("Abre Docker Desktop, espera a que cargue y re-ejecuta start.py")
        else:
            _err(f"Docker no disponible: {out[:120]}")
        return False
    return True


def _container_status() -> str:
    code, out = _run("docker", "inspect", "--format", "{{.State.Status}}", _CONTAINER)
    if code != 0:
        return "missing"
    return "running" if out.strip() == "running" else "stopped"


def _container_key() -> str:
    code, out = _run(
        "docker", "inspect",
        "--format", "{{range .Config.Env}}{{println .}}{{end}}",
        _CONTAINER,
    )
    if code != 0:
        return ""
    for line in out.splitlines():
        if line.startswith("MEILI_MASTER_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def _create_container() -> bool:
    _info(f"Creando contenedor '{_CONTAINER}' con {_MEILI_IMAGE} ...")
    code, out = _run(
        "docker", "run", "-d",
        "--name", _CONTAINER,
        "-p",    f"{_MEILI_PORT}:{_MEILI_PORT}",
        "-e",    f"MEILI_MASTER_KEY={_MEILI_KEY}",
        _MEILI_IMAGE,
    )
    if code != 0:
        _err(f"No se pudo crear el contenedor: {out[:200]}")
        return False
    _ok(f"Contenedor '{_CONTAINER}' creado y arrancado")
    return True


def _remove_container() -> None:
    _run("docker", "stop", _CONTAINER)
    _run("docker", "rm",   _CONTAINER)


def ensure_meilisearch() -> bool:
    _header("Fase 2 — Meilisearch")

    if not _docker_ok():
        return False

    status = _container_status()

    if status == "running":
        if _container_key() == _MEILI_KEY:
            _ok(f"Contenedor '{_CONTAINER}' corriendo con la clave correcta")
            return True
        _warn("Clave del contenedor no coincide con .env — recreando ...")
        _remove_container()
        return _create_container()

    if status == "stopped":
        if _container_key() != _MEILI_KEY:
            _warn("Clave del contenedor detenido diferente — recreando ...")
            _remove_container()
            return _create_container()
        _info(f"Iniciando contenedor '{_CONTAINER}' detenido ...")
        code, out = _run("docker", "start", _CONTAINER)
        if code != 0:
            _err(f"No se pudo iniciar el contenedor: {out[:200]}")
            return False
        _ok(f"Contenedor '{_CONTAINER}' iniciado")
        return True

    return _create_container()


# ─────────────────────────────────────────────────────────────────────────────
# FASE 3 — Health check Meilisearch
# ─────────────────────────────────────────────────────────────────────────────

def wait_for_meilisearch(timeout: int = 40) -> bool:
    _header("Fase 3 — Health check Meilisearch")
    url      = f"{_MEILI_HOST}/health"
    deadline = time.monotonic() + timeout
    dots     = 0

    _info(f"Esperando respuesta en {url}  (max {timeout}s) ...")
    print("       ", end="", flush=True)

    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    print(f"  listo ({dots + 1}s)")
                    _ok("Meilisearch responde correctamente")
                    return True
        except Exception:
            pass
        print(".", end="", flush=True)
        dots += 1
        time.sleep(1)

    print()
    _err(f"Meilisearch no respondio en {timeout}s")
    _warn("La aplicacion arrancara en modo degradado (sin busqueda local)")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# FASE 4 — Servidor web
# ─────────────────────────────────────────────────────────────────────────────

def _verify_tls(cert: Path, key: Path) -> bool:
    """Verifica que el par cert/key es valido y que la clave corresponde al certificado."""
    try:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        return True
    except ssl.SSLError as e:
        _warn(f"Par TLS invalido: {e}")
        return False
    except Exception as e:
        _warn(f"No se pudo verificar el par TLS: {e}")
        return False


def start_server() -> None:
    _header("Fase 4 — Servidor web")

    from backend.config import get_settings
    settings = get_settings()

    port = settings.HTTP3_PORT
    cert = ROOT / settings.TLS_CERT_FILE
    key  = ROOT / settings.TLS_KEY_FILE

    # ── Modo desarrollo: uvicorn con recarga automatica (HTTP) ────────────────
    if _DEV_MODE:
        _info("Modo --dev: uvicorn HTTP con recarga automatica")
        _ok(f"http://localhost:{port}")
        _warn("HTTP/3 NO activo en modo --dev (requiere TLS + Hypercorn)")
        _info("Para HTTP/3:  python start.py   (sin --dev)")
        print()
        cmd = [
            sys.executable, "-m", "uvicorn",
            "Pyton Backend.main:app",
            "--reload",
            "--port", str(port),
            "--host", "127.0.0.1",
            "--log-level", "info",
        ]
        try:
            subprocess.run(cmd, cwd=str(ROOT))
        except KeyboardInterrupt:
            pass
        return

    # ── Modo produccion: Hypercorn con HTTPS + HTTP/3 ─────────────────────────
    try:
        import hypercorn.asyncio
        from hypercorn.config import Config as HypercornConfig
    except ImportError:
        _err("Hypercorn no instalado. Ejecuta:  pip install 'hypercorn[h3]'")
        sys.exit(1)

    cfg          = HypercornConfig()
    cfg.bind     = [f"0.0.0.0:{port}"]   # TCP: HTTP/1.1 + HTTP/2
    cfg.loglevel = "debug" if ENV.get("DEBUG", "").lower() == "true" else "info"
    cfg.accesslog = "-"
    cfg.errorlog  = "-"
    cfg.workers   = 1

    has_certs = cert.exists() and key.exists()

    if has_certs:
        _info("Verificando par certificado/clave ...")
        tls_ok = _verify_tls(cert, key)
        if not tls_ok:
            _err("El certificado y la clave privada no coinciden.")
            _err("Regenera los certificados:  python gen_certs.py")
            sys.exit(1)

        cfg.certfile  = str(cert)
        cfg.keyfile   = str(key)
        cfg.quic_bind = [f"0.0.0.0:{port}"]   # UDP: HTTP/3 (QUIC)

        _ok(f"TLS verificado  ({cert.name} + {key.name})")
        _ok(f"TCP  https://localhost:{port}   (HTTP/1.1 + HTTP/2)")
        _ok(f"UDP  https://localhost:{port}   (HTTP/3 QUIC)")
        _ok("Alt-Svc activo  -> el navegador escalara a HTTP/3 automaticamente")
        print()
        _info("Para verificar HTTP/3 en Chrome:")
        _info("  DevTools -> Network -> columna Protocol -> debe mostrar 'h3'")
        _info("  O visita:  chrome://net-internals/#quic")
    else:
        _warn(f"Certificados no encontrados en '{ROOT / 'certs'}'")
        _warn("HTTP/3 INACTIVO — sirviendo HTTP plano en Hypercorn")
        _info("Genera certificados:  python gen_certs.py")
        cfg.bind = [f"127.0.0.1:{port}"]

    print()

    from backend.main import app
    try:
        asyncio.run(hypercorn.asyncio.serve(app, cfg))
    except KeyboardInterrupt:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Punto de entrada
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    _header("BookDork — Arranque orquestado")
    _info(f"Directorio: {ROOT}")
    mode = "desarrollo (--dev, HTTP)" if _DEV_MODE else "produccion (Hypercorn, HTTPS + HTTP/3)"
    _info(f"Modo      : {mode}")
    _info(f"Meilisearch: {_MEILI_HOST}")

    meili_ok = ensure_meilisearch()

    if meili_ok:
        wait_for_meilisearch()

    start_server()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Servidor detenido.\n")
