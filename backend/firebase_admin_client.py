"""
=============================================================================
firebase_admin_client.py — Firebase Admin SDK: inicialización y operaciones
=============================================================================
Expone helpers privilegiados para operaciones de backend que requieren
acceso total a Firebase Auth y Firestore (sin pasar por reglas de seguridad).

Carga credenciales en este orden de prioridad:
  1. JSON literal en FIREBASE_SERVICE_ACCOUNT_JSON  (ideal para Docker / CI)
  2. Ruta de archivo en FIREBASE_SERVICE_ACCOUNT_PATH (ideal para desarrollo)

El Admin SDK ignora las reglas de Firestore — estas funciones NUNCA deben
ser accesibles desde el cliente; solo desde endpoints protegidos por admin key.
=============================================================================
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from enum import Enum

import firebase_admin
from firebase_admin import auth as fb_auth
from firebase_admin import credentials
from firebase_admin import firestore as fb_firestore

from .config import get_settings
from .firebase_auth import Role, resolve_role

logger = logging.getLogger("bookdork.firebase_admin")

_COLLECTION = "usuarios"
_APP_NAME   = "bookdork-admin"

# ─────────────────────────────────────────────────────────────────────────────
# Enumerado de planes (fuente de verdad compartida con firebase_auth.py)
# ─────────────────────────────────────────────────────────────────────────────

class Plan(str, Enum):
    FREE  = "gratis"
    BASIC = "basic"
    PRO   = "pro"

    @classmethod
    def values(cls) -> list[str]:
        return [p.value for p in cls]


# ─────────────────────────────────────────────────────────────────────────────
# Inicialización singleton
# ─────────────────────────────────────────────────────────────────────────────

def _build_credential() -> credentials.Base:
    """
    Construye la credencial del service account desde la configuración.
    Prioridad: JSON literal en env > archivo en ruta configurada.
    """
    settings = get_settings()

    json_str = (settings.FIREBASE_SERVICE_ACCOUNT_JSON or "").strip()
    if json_str:
        try:
            sa_dict = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "FIREBASE_SERVICE_ACCOUNT_JSON contiene JSON inválido. "
                "Asegúrate de que el valor no tiene saltos de línea sin escapar."
            ) from exc
        return credentials.Certificate(sa_dict)

    path = (settings.FIREBASE_SERVICE_ACCOUNT_PATH or "").strip()
    if path:
        if not os.path.isfile(path):
            raise RuntimeError(
                f"FIREBASE_SERVICE_ACCOUNT_PATH apunta a un archivo inexistente: {path!r}. "
                "Descarga el JSON desde Firebase Console → Configuración del proyecto "
                "→ Cuentas de servicio → Generar nueva clave privada."
            )
        return credentials.Certificate(path)

    raise RuntimeError(
        "Firebase Admin SDK no está configurado. "
        "Define FIREBASE_SERVICE_ACCOUNT_PATH (ruta al JSON) o "
        "FIREBASE_SERVICE_ACCOUNT_JSON (contenido del JSON) en tu archivo .env. "
        "Descarga las credenciales desde Firebase Console → Configuración del proyecto "
        "→ Cuentas de servicio."
    )


def initialize_admin_sdk() -> None:
    """
    Inicializa el Admin SDK una sola vez (idempotente).
    Llamar desde el lifespan de FastAPI al arrancar el servidor.

    Raises:
        RuntimeError  si las credenciales no están configuradas o son inválidas.
    """
    settings = get_settings()

    # Si ya está inicializado, no hacer nada
    try:
        firebase_admin.get_app(_APP_NAME)
        logger.debug("Firebase Admin SDK ya estaba inicializado.")
        return
    except ValueError:
        pass  # La app no existe aún — continuar con la inicialización

    cred = _build_credential()
    firebase_admin.initialize_app(
        cred,
        name=_APP_NAME,
        options={"projectId": settings.FIREBASE_PROJECT_ID},
    )
    logger.info(
        "Firebase Admin SDK inicializado correctamente (proyecto: %s).",
        settings.FIREBASE_PROJECT_ID,
    )


def _get_app() -> firebase_admin.App:
    """Devuelve la app Admin SDK inicializada o lanza RuntimeError."""
    try:
        return firebase_admin.get_app(_APP_NAME)
    except ValueError:
        raise RuntimeError(
            "Firebase Admin SDK no está inicializado. "
            "Verifica que FIREBASE_SERVICE_ACCOUNT_PATH o "
            "FIREBASE_SERVICE_ACCOUNT_JSON están definidos en .env."
        )


def _get_db():
    """Devuelve el cliente Firestore del Admin SDK."""
    return fb_firestore.client(app=_get_app())


# ─────────────────────────────────────────────────────────────────────────────
# Operaciones de Auth (wrappers async sobre la API síncrona del SDK)
# ─────────────────────────────────────────────────────────────────────────────

async def get_user_by_email(email: str) -> fb_auth.UserRecord:
    """
    Obtiene el UserRecord de Firebase Auth por email.

    Raises:
        ValueError      si el usuario no existe.
        RuntimeError    si el Admin SDK no está inicializado.
    """
    app = _get_app()
    try:
        return await asyncio.to_thread(fb_auth.get_user_by_email, email, app)
    except fb_auth.UserNotFoundError:
        raise ValueError(f"No existe ningún usuario con el email {email!r}.")


async def get_user_by_uid(uid: str) -> fb_auth.UserRecord:
    """
    Obtiene el UserRecord de Firebase Auth por UID.

    Raises:
        ValueError      si el usuario no existe.
        RuntimeError    si el Admin SDK no está inicializado.
    """
    app = _get_app()
    try:
        return await asyncio.to_thread(fb_auth.get_user, uid, app)
    except fb_auth.UserNotFoundError:
        raise ValueError(f"No existe ningún usuario con el UID {uid!r}.")


# ─────────────────────────────────────────────────────────────────────────────
# Operaciones de Firestore
# ─────────────────────────────────────────────────────────────────────────────

async def get_firestore_user(uid: str) -> dict:
    """
    Obtiene el documento de usuario desde Firestore.
    Devuelve {} si el documento no existe todavía.
    """
    def _read() -> dict:
        doc = _get_db().collection(_COLLECTION).document(uid).get()
        return doc.to_dict() or {}

    return await asyncio.to_thread(_read)


async def set_user_plan(uid: str, new_plan: Plan, changed_by: str) -> dict:
    """
    Cambia el plan de un usuario en Firestore de forma atómica.

    Solo actualiza el campo `plan` — no toca contadores de conversiones.
    Verifica primero que el usuario existe en Firebase Auth.

    Args:
        uid:        UID del usuario.
        new_plan:   Nuevo plan a asignar.
        changed_by: Identificador del actor (IP o alias) para auditoría.

    Returns:
        dict con plan_anterior y plan_nuevo.

    Raises:
        ValueError      si el usuario no existe en Firebase Auth.
        RuntimeError    si el Admin SDK no está inicializado.
    """
    # Verificar que el usuario existe antes de tocar Firestore
    await get_user_by_uid(uid)

    def _write() -> dict:
        ref      = _get_db().collection(_COLLECTION).document(uid)
        prev_doc = ref.get().to_dict() or {}
        prev_val = prev_doc.get("plan", Plan.FREE.value)

        # Normalizar el valor anterior al enum (tolerante con datos legacy)
        try:
            prev_plan = Plan(prev_val)
        except ValueError:
            prev_plan = Plan.FREE

        ref.set({"plan": new_plan.value}, merge=True)
        return {"plan_anterior": prev_plan, "plan_nuevo": new_plan}

    result = await asyncio.to_thread(_write)

    logger.info(
        "AUDIT set_plan — uid=%s plan_anterior=%s plan_nuevo=%s changed_by=%s",
        uid[:8],
        result["plan_anterior"].value,
        result["plan_nuevo"].value,
        changed_by,
    )
    return result


async def update_user_conversions(uid: str, fields: dict) -> None:
    """
    Escribe contadores de conversión en Firestore usando el Admin SDK
    (privilegiado — no está sujeto a las reglas de seguridad de Firestore).

    Args:
        uid:    UID del usuario.
        fields: Campos a actualizar (merge=True, no toca el resto del doc).

    Raises:
        RuntimeError si el Admin SDK no está inicializado.
    """
    def _write() -> None:
        _get_db().collection(_COLLECTION).document(uid).set(fields, merge=True)

    await asyncio.to_thread(_write)


# ─────────────────────────────────────────────────────────────────────────────
# RBAC — Gestión de roles administrativos (Firebase custom claims)
# ─────────────────────────────────────────────────────────────────────────────

async def get_user_role(uid: str) -> Role:
    """
    Devuelve el rol administrativo del usuario leyendo sus custom claims.
    Un usuario sin claim 'role' es Role.USER (sin privilegios admin).

    Raises:
        ValueError    si el usuario no existe en Firebase Auth.
        RuntimeError  si el Admin SDK no está inicializado.
    """
    record = await get_user_by_uid(uid)
    return resolve_role((record.custom_claims or {}).get("role"))


async def set_user_role(
    uid: str,
    new_role: Role,
    changed_by: str,
    revoke_sessions: bool = True,
) -> dict:
    """
    Asigna un rol administrativo a un usuario vía custom claims del Admin SDK.

    El claim 'role' se incrusta en los futuros Firebase ID tokens del usuario y
    es verificado localmente por el backend (require_admin_access). Esto sustituye
    a la clave admin compartida por una identidad individual, auditable y revocable.

    Detalles de diseño:
      • Preserva cualquier otro custom claim ya presente (no los pisa).
      • Role.USER se modela como AUSENCIA del claim 'role' (limpieza, no "user").
      • revoke_sessions=True invalida los refresh tokens para forzar re-login y
        que el nuevo claim se propague. Nota de propagación: un ID token ya emitido
        sigue siendo válido hasta su expiración (≤1 h); por eso, al RETIRAR
        privilegios existe una ventana de ≤1 h en la que el token viejo aún
        contiene el rol anterior. Para revocación inmediata, además de revocar
        sesiones habría que verificar el estado de revocación contra el Admin SDK
        en cada request (no implementado por su coste; ≤1 h es aceptable aquí).

    Args:
        uid:             UID del usuario objetivo.
        new_role:        Rol a asignar.
        changed_by:      Identidad del actor (para auditoría).
        revoke_sessions: Si True, revoca refresh tokens tras el cambio.

    Returns:
        dict con 'role_anterior' y 'role_nuevo' (ambos Role).

    Raises:
        ValueError    si el usuario no existe en Firebase Auth.
        RuntimeError  si el Admin SDK no está inicializado.
    """
    app    = _get_app()
    record = await get_user_by_uid(uid)  # valida existencia (ValueError si no)
    existing  = dict(record.custom_claims or {})
    prev_role = resolve_role(existing.get("role"))

    def _write() -> None:
        new_claims = dict(existing)
        if new_role == Role.USER:
            new_claims.pop("role", None)        # 'user' = sin claim
        else:
            new_claims["role"] = new_role.value
        # set_custom_user_claims acepta None para borrar todos los claims; si el
        # dict queda vacío pasamos None para no almacenar un objeto vacío.
        fb_auth.set_custom_user_claims(uid, new_claims or None, app=app)
        if revoke_sessions:
            fb_auth.revoke_refresh_tokens(uid, app=app)

    await asyncio.to_thread(_write)

    logger.info(
        "AUDIT set_role — uid=%s role_anterior=%s role_nuevo=%s changed_by=%s",
        uid[:8],
        prev_role.value,
        new_role.value,
        changed_by,
    )
    return {"role_anterior": prev_role, "role_nuevo": new_role}
