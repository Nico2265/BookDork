"""
gen_certs.py — Genera certificado TLS local de confianza usando mkcert.

mkcert crea una CA local reconocida por Chrome, Edge y Firefox, lo que
permite HTTPS con candado verde y HTTP/3 (QUIC) sin advertencias de seguridad.

Uso:
    python gen_certs.py

Requiere mkcert instalado:
    winget install FiloSottile.mkcert   (Windows)
    brew install mkcert                 (macOS)
    sudo apt install mkcert             (Debian/Ubuntu)

Genera:
    certs/server.crt   Certificado firmado por la CA local de mkcert
    certs/server.key   Clave privada RSA

SANs incluidos: localhost, 127.0.0.1, ::1
"""

import pathlib
import subprocess
import sys

ROOT      = pathlib.Path(__file__).parent
CERTS_DIR = ROOT / "certs"
CERT_FILE = CERTS_DIR / "server.crt"
KEY_FILE  = CERTS_DIR / "server.key"

_W = 54


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def _line(msg: str) -> None:
    print(f"  {msg}")


def main() -> None:
    print()
    print("=" * _W)
    print("  BookDork — Generador de certificado TLS / HTTP3")
    print("=" * _W)

    # ── Verificar mkcert ────────────────────────────────────────────────────────
    check = _run(["mkcert", "-version"])
    if check.returncode != 0:
        _line("ERROR: mkcert no encontrado.")
        _line("Instalalo con:  winget install FiloSottile.mkcert")
        sys.exit(1)

    version = (check.stdout + check.stderr).strip()
    _line(f"mkcert {version} detectado")

    CERTS_DIR.mkdir(exist_ok=True)

    # ── Instalar CA local en el trust store del sistema ─────────────────────────
    _line("Instalando CA local en el trust store del sistema...")
    install = _run(["mkcert", "-install"])
    if install.returncode == 0:
        _line("CA local instalada (Chrome/Edge/Firefox la reconocen automaticamente)")
    else:
        _line("ADVERTENCIA: no se pudo instalar la CA automaticamente.")
        _line("Ejecuta 'mkcert -install' como Administrador y vuelve a intentarlo.")

    # ── Generar certificado con SANs completos ──────────────────────────────────
    _line("Generando certificado para localhost + 127.0.0.1 + ::1 ...")
    gen = _run([
        "mkcert",
        "-key-file",  str(KEY_FILE),   # certs/server.key
        "-cert-file", str(CERT_FILE),  # certs/server.crt
        "localhost",
        "127.0.0.1",
        "::1",
    ])

    if gen.returncode != 0:
        output = (gen.stdout + gen.stderr).strip()
        _line(f"ERROR al generar certificado: {output}")
        sys.exit(1)

    # ── Verificar que ambos archivos existen y tienen contenido ─────────────────
    for path in (CERT_FILE, KEY_FILE):
        if not path.exists() or path.stat().st_size == 0:
            _line(f"ERROR: {path.name} no fue generado correctamente.")
            sys.exit(1)

    # ── Inspeccionar el certificado generado ────────────────────────────────────
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        cert = x509.load_pem_x509_certificate(CERT_FILE.read_bytes(), default_backend())
        sans = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
        ips  = cert.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.IPAddress)
        _line(f"SANs DNS : {', '.join(sans)}")
        _line(f"SANs IP  : {', '.join(str(ip) for ip in ips)}")
        _line(f"Valido hasta: {cert.not_valid_after_utc.date()}")
    except Exception:
        pass  # La inspección es informativa, no crítica

    print()
    print("=" * _W)
    _line(f"Certificado : {CERT_FILE}")
    _line(f"Clave       : {KEY_FILE}")
    print("=" * _W)
    print()
    _line("Siguiente paso:")
    _line("  python start.py        -> arranca con HTTPS + HTTP/3")
    _line("")
    _line("Para verificar HTTP/3 en Chrome:")
    _line("  Abre DevTools -> Network -> columna Protocol -> debe mostrar 'h3'")
    print()


if __name__ == "__main__":
    main()
