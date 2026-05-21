"""
=============================================================================
admin_routes.py — Endpoints de administración de usuarios y planes
=============================================================================
Todos los endpoints requieren la cabecera X-Admin-API-Key.
Operan sobre Firebase Auth y Firestore usando el Admin SDK (privilegiado).

Rutas disponibles:
  GET  /api/admin/users/by-email          → Busca usuario por email
  GET  /api/admin/users/{uid}             → Busca usuario por UID
  PATCH /api/admin/users/by-email/plan    → Cambia plan por email
  PATCH /api/admin/users/{uid}/plan       → Cambia plan por UID
=============================================================================
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, field_validator  # EmailStr requiere: pip install email-validator

from .firebase_admin_client import (
    Plan,
    get_firestore_user,
    get_user_by_email,
    get_user_by_uid,
    set_user_plan,
)
from .security import require_admin_key

logger  = logging.getLogger("bookdork.admin")
router  = APIRouter(prefix="/api/admin", tags=["Administración — Usuarios"])

_PLAN_DESCRIPTIONS: dict[str, str] = {
    Plan.FREE.value:  "5 conversiones totales (lifetime)",
    Plan.BASIC.value: "50 conversiones/día",
    Plan.PRO.value:   "200 conversiones/día",
}


# ─────────────────────────────────────────────────────────────────────────────
# Modelos de solicitud / respuesta
# ─────────────────────────────────────────────────────────────────────────────

class SetPlanRequest(BaseModel):
    plan: Plan

    @field_validator("plan", mode="before")
    @classmethod
    def normalize_plan(cls, v: str) -> str:
        """Acepta mayúsculas/minúsculas para mayor usabilidad."""
        return v.lower() if isinstance(v, str) else v


class UserAdminResponse(BaseModel):
    """Vista administrativa de un usuario: datos de Auth + Firestore."""
    uid: str
    email: Optional[str]
    display_name: Optional[str]
    disabled: bool
    plan: str
    plan_descripcion: str
    conversiones_usadas: int
    conversiones_diarias: int
    fecha_conversiones: Optional[str]


class SetPlanResponse(BaseModel):
    uid: str
    email: Optional[str]
    plan_anterior: str
    plan_nuevo: str
    plan_descripcion: str
    mensaje: str


# ─────────────────────────────────────────────────────────────────────────────
# Helper interno
# ─────────────────────────────────────────────────────────────────────────────

def _caller_id(request: Request) -> str:
    """Devuelve IP del llamador para el log de auditoría."""
    if request.client:
        return request.client.host
    return "unknown"


def _wrap_admin_errors(exc: Exception) -> HTTPException:
    """Convierte excepciones internas en respuestas HTTP apropiadas."""
    if isinstance(exc, ValueError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RuntimeError):
        return HTTPException(status_code=503, detail=str(exc))
    logger.exception("Error inesperado en admin_routes: %s", exc)
    return HTTPException(status_code=500, detail="Error interno del servidor.")


async def _build_user_response(uid: str) -> UserAdminResponse:
    """Combina Firebase Auth + Firestore en un único objeto de respuesta."""
    user_record = await get_user_by_uid(uid)
    fs_data     = await get_firestore_user(uid)

    plan_val = fs_data.get("plan", Plan.FREE.value)
    try:
        Plan(plan_val)  # Validar que el valor es conocido
    except ValueError:
        plan_val = Plan.FREE.value

    return UserAdminResponse(
        uid                  = user_record.uid,
        email                = user_record.email,
        display_name         = user_record.display_name,
        disabled             = user_record.disabled,
        plan                 = plan_val,
        plan_descripcion     = _PLAN_DESCRIPTIONS.get(plan_val, plan_val),
        conversiones_usadas  = fs_data.get("conversiones", 0),
        conversiones_diarias = fs_data.get("conversionesDiarias", 0),
        fecha_conversiones   = fs_data.get("fechaConversiones"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET — Consultar usuario
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/users/by-email",
    response_model=UserAdminResponse,
    summary="Obtiene información de un usuario por email",
)
async def get_user_info_by_email(
    request: Request,
    email: EmailStr = Query(..., description="Email del usuario"),
    _: str = Depends(require_admin_key),
) -> UserAdminResponse:
    """
    Devuelve datos de Firebase Auth y Firestore del usuario con ese email.
    """
    try:
        user_record = await get_user_by_email(email)
    except (ValueError, RuntimeError) as exc:
        raise _wrap_admin_errors(exc)

    logger.info(
        "AUDIT get_user — email=%s uid=%s ip=%s",
        email, user_record.uid[:8], _caller_id(request),
    )

    try:
        return await _build_user_response(user_record.uid)
    except (ValueError, RuntimeError) as exc:
        raise _wrap_admin_errors(exc)


@router.get(
    "/users/{uid}",
    response_model=UserAdminResponse,
    summary="Obtiene información de un usuario por UID",
)
async def get_user_info_by_uid(
    request: Request,
    uid: str,
    _: str = Depends(require_admin_key),
) -> UserAdminResponse:
    """
    Devuelve datos de Firebase Auth y Firestore del usuario con ese UID.
    """
    logger.info(
        "AUDIT get_user — uid=%s ip=%s",
        uid[:8], _caller_id(request),
    )

    try:
        return await _build_user_response(uid)
    except (ValueError, RuntimeError) as exc:
        raise _wrap_admin_errors(exc)


# ─────────────────────────────────────────────────────────────────────────────
# PATCH — Cambiar plan
# ─────────────────────────────────────────────────────────────────────────────

@router.patch(
    "/users/by-email/plan",
    response_model=SetPlanResponse,
    summary="Cambia el plan de suscripción de un usuario por email",
)
async def set_plan_by_email(
    request: Request,
    body: SetPlanRequest,
    email: EmailStr = Query(..., description="Email del usuario"),
    _: str = Depends(require_admin_key),
) -> SetPlanResponse:
    """
    Actualiza el campo `plan` en Firestore para el usuario con ese email.

    Planes disponibles:
    - `gratis` — 5 conversiones totales (lifetime)
    - `basic`  — 50 conversiones/día
    - `pro`    — 200 conversiones/día
    """
    try:
        user_record = await get_user_by_email(email)
    except (ValueError, RuntimeError) as exc:
        raise _wrap_admin_errors(exc)

    try:
        result = await set_user_plan(
            uid        = user_record.uid,
            new_plan   = body.plan,
            changed_by = _caller_id(request),
        )
    except (ValueError, RuntimeError) as exc:
        raise _wrap_admin_errors(exc)

    return SetPlanResponse(
        uid              = user_record.uid,
        email            = user_record.email,
        plan_anterior    = result["plan_anterior"].value,
        plan_nuevo       = result["plan_nuevo"].value,
        plan_descripcion = _PLAN_DESCRIPTIONS.get(body.plan.value, body.plan.value),
        mensaje          = (
            f"Plan actualizado correctamente de "
            f"'{result['plan_anterior'].value}' a '{result['plan_nuevo'].value}'."
        ),
    )


@router.patch(
    "/users/{uid}/plan",
    response_model=SetPlanResponse,
    summary="Cambia el plan de suscripción de un usuario por UID",
)
async def set_plan_by_uid(
    request: Request,
    uid: str,
    body: SetPlanRequest,
    _: str = Depends(require_admin_key),
) -> SetPlanResponse:
    """
    Actualiza el campo `plan` en Firestore para el usuario con ese UID.

    Planes disponibles:
    - `gratis` — 5 conversiones totales (lifetime)
    - `basic`  — 50 conversiones/día
    - `pro`    — 200 conversiones/día
    """
    try:
        result      = await set_user_plan(
            uid        = uid,
            new_plan   = body.plan,
            changed_by = _caller_id(request),
        )
        user_record = await get_user_by_uid(uid)
    except (ValueError, RuntimeError) as exc:
        raise _wrap_admin_errors(exc)

    return SetPlanResponse(
        uid              = uid,
        email            = user_record.email,
        plan_anterior    = result["plan_anterior"].value,
        plan_nuevo       = result["plan_nuevo"].value,
        plan_descripcion = _PLAN_DESCRIPTIONS.get(body.plan.value, body.plan.value),
        mensaje          = (
            f"Plan actualizado correctamente de "
            f"'{result['plan_anterior'].value}' a '{result['plan_nuevo'].value}'."
        ),
    )
