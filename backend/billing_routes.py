"""
=============================================================================
billing_routes.py — Endpoints de facturación del propio usuario
=============================================================================
A diferencia de admin_routes.py (operaciones de un administrador sobre CUALQUIER
usuario), estas rutas las invoca el PROPIO usuario autenticado sobre su propia
suscripción. Todas exigen un Firebase ID token válido (Authorization: Bearer).

Flujo de pago simulado en dos pasos (intención → confirmación):

  POST /api/billing/checkout      → emite un token de pago firmado
  POST /api/billing/confirm       → verifica el token y activa/renueva el plan
  POST /api/billing/cancel        → cancela la renovación (acceso hasta vencer)
  GET  /api/billing/subscription  → estado actual de la suscripción (server-side)

⚠️  Es una SIMULACIÓN: no se contacta ninguna pasarela ni se cobra nada real.
    En producción, checkout/confirm deben integrarse con la pasarela y validar
    su webhook firmado antes de activar el plan.
=============================================================================
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from . import billing
from .firebase_admin_client import (
    Plan,
    activate_subscription,
    cancel_subscription,
    get_firestore_user,
)
from .firebase_auth import AuthContext, get_auth_context
from .security import check_user_agent

logger = logging.getLogger("bookdork.billing")
router = APIRouter(prefix="/api/billing", tags=["Facturación"])


# ─────────────────────────────────────────────────────────────────────────────
# Modelos
# ─────────────────────────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    plan: str

    @field_validator("plan", mode="before")
    @classmethod
    def normalize(cls, v: object) -> object:
        return v.lower().strip() if isinstance(v, str) else v


class CheckoutResponse(BaseModel):
    token: str
    transaction_id: str
    plan: str
    plan_nombre: str
    monto: int
    expira_en: str


class ConfirmRequest(BaseModel):
    token: str


class SubscriptionResponse(BaseModel):
    plan: str
    plan_almacenado: str
    estado: str
    renovacion_automatica: bool
    fecha_compra: Optional[str]
    fecha_vencimiento: Optional[str]
    precio: Optional[int]
    monto_total: Optional[int]
    ultimo_pago_id: Optional[str]
    transaction_id: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Paso 1 — Emitir token de pago (intención)
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/checkout",
    response_model=CheckoutResponse,
    summary="Emite un token de pago firmado para activar un plan",
)
async def billing_checkout(
    request: Request,
    body: CheckoutRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> CheckoutResponse:
    """
    Genera un token de pago para el plan solicitado, asociado al usuario
    autenticado. El token NO activa el plan: debe confirmarse en /confirm.
    """
    check_user_agent(request)

    if not billing.is_billable_plan(body.plan):
        raise HTTPException(
            status_code=400,
            detail="Plan no válido. Solo 'basic' o 'pro' pueden comprarse.",
        )

    pt = billing.issue_payment_token(uid=auth.uid, plan=body.plan)
    logger.info(
        "Billing checkout — uid=%s plan=%s txn=%s monto=%s",
        auth.uid[:8], pt.plan, pt.transaction_id, pt.amount,
    )
    return CheckoutResponse(
        token=pt.token,
        transaction_id=pt.transaction_id,
        plan=pt.plan,
        plan_nombre=billing.PLAN_PRICES[pt.plan]["name"],
        monto=pt.amount,
        expira_en=pt.expires_at.isoformat(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Paso 2 — Verificar token y activar el plan
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/confirm",
    response_model=SubscriptionResponse,
    summary="Verifica el token de pago y activa/renueva el plan del usuario",
)
async def billing_confirm(
    request: Request,
    body: ConfirmRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> SubscriptionResponse:
    """
    Verifica la firma y vigencia del token de pago (y que pertenezca al usuario
    autenticado) y SOLO entonces actualiza el estado del plan en Firestore.

    Es el punto donde "al realizar el pago, el estado del plan se actualiza":
    la activación es atómica y server-side, imposible de falsear desde el
    cliente porque exige un token firmado por el servidor.
    """
    check_user_agent(request)

    try:
        pt = billing.verify_payment_token(body.token, expected_uid=auth.uid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        dates = await activate_subscription(
            uid            = auth.uid,
            plan           = Plan(pt.plan),
            transaction_id = pt.transaction_id,
            payment_token  = pt.token,
            amount         = pt.amount,
            period_days    = billing.BILLING_PERIOD_DAYS,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        logger.error("Activación de plan falló uid=%s: %s", auth.uid[:8], exc)
        raise HTTPException(
            status_code=503,
            detail="No se pudo activar el plan. El pago no se aplicó; intenta más tarde.",
        )

    # Releer el documento para devolver el estado CANÓNICO ya persistido
    # (verificación de que el plan efectivamente se actualizó).
    data  = await get_firestore_user(auth.uid)
    state = billing.build_subscription_state(data)
    state["transaction_id"] = pt.transaction_id

    logger.info(
        "Billing confirmed — uid=%s plan=%s estado=%s vence=%s",
        auth.uid[:8], state["plan"], state["estado"], dates["fecha_vencimiento"],
    )
    return SubscriptionResponse(**state)


# ─────────────────────────────────────────────────────────────────────────────
# Cancelar suscripción mensual
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/cancel",
    response_model=SubscriptionResponse,
    summary="Cancela la renovación automática (acceso hasta el vencimiento)",
)
async def billing_cancel(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> SubscriptionResponse:
    """
    Cancela la facturación mensual. El usuario conserva su plan hasta la fecha
    de vencimiento; luego degrada a 'gratis' automáticamente.
    """
    check_user_agent(request)

    try:
        await cancel_subscription(auth.uid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        logger.error("Cancelación falló uid=%s: %s", auth.uid[:8], exc)
        raise HTTPException(
            status_code=503,
            detail="No se pudo cancelar la suscripción. Intenta más tarde.",
        )

    data  = await get_firestore_user(auth.uid)
    state = billing.build_subscription_state(data)
    return SubscriptionResponse(**state)


# ─────────────────────────────────────────────────────────────────────────────
# Consultar estado de la suscripción
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/subscription",
    response_model=SubscriptionResponse,
    summary="Devuelve el estado actual de la suscripción del usuario",
)
async def billing_subscription(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> SubscriptionResponse:
    """
    Fuente de verdad para la vista de cuenta: calcula el plan efectivo, su
    vigencia y las fechas de compra/vencimiento desde Firestore (server-side).
    """
    check_user_agent(request)

    try:
        data = await get_firestore_user(auth.uid)
    except RuntimeError as exc:
        logger.error("Lectura de suscripción falló uid=%s: %s", auth.uid[:8], exc)
        raise HTTPException(
            status_code=503,
            detail="No se pudo cargar tu suscripción. Intenta más tarde.",
        )

    return SubscriptionResponse(**billing.build_subscription_state(data))
