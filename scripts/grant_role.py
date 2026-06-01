#!/usr/bin/env python
"""
=============================================================================
grant_role.py — Gestión de roles RBAC (custom claims de Firebase) por CLI
=============================================================================
Asigna o consulta el rol administrativo de un usuario usando el Admin SDK
DIRECTAMENTE, sin necesidad de que el servidor esté corriendo ni de la
X-Admin-API-Key. Es la vía recomendada para el BOOTSTRAP del primer
'superadmin' (más segura que exponer la clave admin por HTTP).

Requisitos:
    Service account configurado en .env (igual que /api/admin/*):
        FIREBASE_SERVICE_ACCOUNT_PATH=ruta/al/serviceAccount.json
      o FIREBASE_SERVICE_ACCOUNT_JSON={...contenido...}

Uso:
    # Bootstrap del primer superadmin
    python scripts/grant_role.py --email tu@correo.com --role superadmin

    # Asignar por UID
    python scripts/grant_role.py --uid abcd1234 --role billing

    # Retirar privilegios (vuelve a usuario normal)
    python scripts/grant_role.py --email tu@correo.com --role user

    # Consultar el rol actual sin modificar
    python scripts/grant_role.py --email tu@correo.com --show

Códigos de salida: 0 ok · 1 args · 2 SDK no configurado · 3 usuario inexistente
=============================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import socket
import sys
from pathlib import Path

# Permite ejecutar el script desde cualquier directorio: añade la raíz del repo
# al sys.path para poder importar el paquete `backend`.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.firebase_auth import Role  # noqa: E402
from backend.firebase_admin_client import (  # noqa: E402
    get_user_by_email,
    get_user_role,
    initialize_admin_sdk,
    set_user_role,
)


def _actor() -> str:
    """Identidad del operador para el registro de auditoría."""
    try:
        return f"{getpass.getuser()}@{socket.gethostname()}"
    except Exception:
        return "unknown"


async def _run(args: argparse.Namespace) -> int:
    initialize_admin_sdk()  # RuntimeError si no hay service account

    # ── Resolver UID (por email o directo) ────────────────────────────────────
    if args.email:
        record = await get_user_by_email(args.email)  # ValueError si no existe
        uid    = record.uid
        who    = f"{args.email} (uid={uid[:8]}…)"
    else:
        uid = args.uid
        who = f"uid={uid[:8]}…"

    # ── Solo consultar ────────────────────────────────────────────────────────
    if args.show:
        role = await get_user_role(uid)
        print(f"Rol actual de {who}: {role.value}")
        return 0

    # ── Asignar rol ───────────────────────────────────────────────────────────
    new_role = Role(args.role)
    result   = await set_user_role(uid, new_role, changed_by=f"cli:{_actor()}")
    print(
        f"Rol de {who}: "
        f"{result['role_anterior'].value} → {result['role_nuevo'].value}"
    )
    print(
        "Nota: el cambio se propaga al refrescar el ID token (≤1 h); las "
        "sesiones activas se revocaron para forzar re-login."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Gestiona roles RBAC (custom claims de Firebase) sin el servidor. "
            "Útil para el bootstrap del primer superadmin."
        )
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--email", help="Email del usuario objetivo")
    target.add_argument("--uid",   help="UID del usuario objetivo")

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--role",
        choices=Role.values(),
        help="Rol a asignar ('user' retira privilegios admin)",
    )
    action.add_argument(
        "--show",
        action="store_true",
        help="Muestra el rol actual sin modificarlo",
    )

    args = parser.parse_args(argv)

    try:
        return asyncio.run(_run(args))
    except RuntimeError as exc:
        print(f"ERROR de configuración: {exc}", file=sys.stderr)
        print(
            "Define FIREBASE_SERVICE_ACCOUNT_PATH o FIREBASE_SERVICE_ACCOUNT_JSON "
            "en tu .env (Firebase Console → Configuración → Cuentas de servicio).",
            file=sys.stderr,
        )
        return 2
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
