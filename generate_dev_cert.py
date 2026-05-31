r"""
=============================================================================
generate_dev_cert.py — Genera certificado TLS autofirmado para desarrollo
=============================================================================
Requisito: librería 'cryptography' (ya incluida como dep. de aioquic).

Uso:
    python generate_dev_cert.py

Genera:
    certs/server.crt  — Certificado X.509 (RSA-2048, SHA-256, 2 años)
    certs/server.key  — Clave privada (no cifrada, solo para desarrollo)

Pasos después de ejecutar:
  1. Editar .env:  FORCE_HTTP=false
  2. Reiniciar el servidor
  3. Acceder a https://localhost:8000
     → El navegador mostrará advertencia de certificado autofirmado
     → Haz clic en "Avanzado" → "Continuar de todos modos (no seguro)"

Para eliminar la advertencia (certificado confiado localmente):
  ┌─ Opción A: mkcert (recomendado para desarrollo) ─────────────────────────┐
  │  1. Descarga mkcert: https://github.com/FiloSottile/mkcert/releases      │
  │  2. Ejecuta como administrador:                                           │
  │       mkcert -install                                                     │
  │       mkcert -cert-file certs\server.crt -key-file certs\server.key ^    │
  │              localhost 127.0.0.1 ::1                                      │
  │  → El navegador confiará en el cert sin advertencia                       │
  └──────────────────────────────────────────────────────────────────────────┘

  ┌─ Opción B: Producción (Let's Encrypt + dominio propio) ──────────────────┐
  │  1. Apunta un dominio a tu IP (ej. bookdork.tudominio.cl)                │
  │  2. certbot certonly --standalone -d bookdork.tudominio.cl               │
  │  3. Copia /etc/letsencrypt/live/…/fullchain.pem → certs/server.crt      │
  │        /etc/letsencrypt/live/…/privkey.pem   → certs/server.key         │
  │  4. .env: FORCE_HTTP=false, APP_DOMAIN=bookdork.tudominio.cl             │
  └──────────────────────────────────────────────────────────────────────────┘
=============================================================================
"""
import datetime
import ipaddress
import pathlib
import sys

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError:
    sys.exit(
        "Error: librería 'cryptography' no encontrada.\n"
        "Instala con: pip install cryptography"
    )

CERTS_DIR = pathlib.Path("certs")
CERT_FILE = CERTS_DIR / "server.crt"
KEY_FILE  = CERTS_DIR / "server.key"
VALID_DAYS = 730  # 2 años


def generate() -> None:
    CERTS_DIR.mkdir(exist_ok=True)

    print("Generando clave privada RSA-2048…")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME,         "CL"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME,    "BookDork Dev"),
        x509.NameAttribute(NameOID.COMMON_NAME,          "localhost"),
    ])

    now = datetime.datetime.now(datetime.timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                x509.IPAddress(ipaddress.IPv6Address("::1")),
            ]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True,
                content_commitment=False, key_agreement=False,
                key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
                data_encipherment=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    KEY_FILE.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )

    expires = (now + datetime.timedelta(days=VALID_DAYS)).strftime("%d/%m/%Y")
    print(f"\n  Certificado: {CERT_FILE}  (válido hasta {expires})")
    print(f"  Clave:       {KEY_FILE}")
    print()
    print("Pasos siguientes:")
    print("  1. Edita .env y cambia:  FORCE_HTTP=false")
    print("  2. Reinicia el servidor")
    print("  3. Abre https://localhost:8000")
    print()
    print("Nota: el navegador mostrará 'Advertencia de seguridad'.")
    print("Usa mkcert para eliminarla (ver instrucciones al inicio del script).")


if __name__ == "__main__":
    generate()
