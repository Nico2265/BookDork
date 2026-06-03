"""
=============================================================================
billing.py — Lógica de facturación y token de pago (simulación)
=============================================================================
Este módulo concentra TODA la lógica del flujo de pago simulado:

  • Catálogo de planes y precios (fuente de verdad compartida con el frontend).
  • Emisión y verificación de un *token de pago* firmado (HMAC-SHA256).
  • Cálculo del estado de la suscripción (plan efectivo, fechas, vigencia).

────────────────────────────────────────────────────────────────────────────
SOBRE EL "TOKEN DE PAGO"
────────────────────────────────────────────────────────────────────────────
En una pasarela real (Stripe, Transbank…) el navegador NUNCA activa un plan
por sí mismo: tokeniza la tarjeta, el backend crea un *PaymentIntent*, la
pasarela confirma el cargo y un webhook firmado autoriza la activación.

Aquí reproducimos ese patrón en dos pasos, de forma verificable:

  1. checkout()  → el backend emite un token firmado que representa una
                   "intención de pago autorizada" para (uid, plan, monto).
                   El token caduca en TOKEN_TTL_SECONDS.
  2. confirm()   → el backend VERIFICA la firma + caducidad + que el uid del
                   token coincide con el usuario autenticado, y solo entonces
                   actualiza el plan en Firestore (vía Admin SDK).

El token es opaco para el cliente y no puede falsificarse sin la clave de
firma del servidor — exactamente la propiedad que garantiza que "cuando se
realiza un pago, el estado del plan se actualiza" de forma íntegra.

⚠️  PRODUCCIÓN: sustituye checkout()/confirm() por la integración real con la
    pasarela y valida el webhook firmado de ésta antes de activar el plan.
=============================================================================
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass

from .config import get_settings

# ─────────────────────────────────────────────────────────────────────────────
# Catálogo de planes (precios en CLP, alineados con Frontend/checkout.js)
# ─────────────────────────────────────────────────────────────────────────────

TAX_RATE = 0.19  # IVA Chile

# Planes de pago activables desde el checkout (no incluye 'gratis').
PLAN_PRICES: dict[str, dict] = {
    "basic": {"name": "Basic", "price": 4990},
    "pro":   {"name": "Pro",   "price": 9990},
}

# Duración del ciclo de facturación mensual.
BILLING_PERIOD_DAYS = 30

# Validez del token de pago entre checkout() y confirm().
TOKEN_TTL_SECONDS = 600  # 10 minutos

_TOKEN_PREFIX = "bdpay_"


def is_billable_plan(plan: str) -> bool:
    """True si el plan puede comprarse en el checkout (basic/pro)."""
    return plan in PLAN_PRICES


def compute_amount(plan: str) -> int:
    """Total a cobrar (subtotal + IVA), redondeado a CLP entero."""
    price = PLAN_PRICES[plan]["price"]
    return round(price * (1 + TAX_RATE))


# ─────────────────────────────────────────────────────────────────────────────
# Firma del token (HMAC-SHA256)
# ─────────────────────────────────────────────────────────────────────────────

def _signing_key() -> bytes:
    """
    Deriva una clave de firma específica de propósito a partir de ADMIN_API_KEY.
    El prefijo de dominio evita que la clave pueda reutilizarse para firmar otra
    cosa fuera de este contexto (defensa contra confusión de claves).
    """
    admin_key = get_settings().ADMIN_API_KEY.encode("utf-8")
    return hashlib.sha256(b"bookdork-billing-token-v1:" + admin_key).digest()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


@dataclass(frozen=True)
class PaymentToken:
    """Token de pago emitido por el backend (intención de pago autorizada)."""
    token: str
    transaction_id: str
    uid: str
    plan: str
    amount: int
    issued_at: datetime.datetime
    expires_at: datetime.datetime


def issue_payment_token(uid: str, plan: str) -> PaymentToken:
    """
    Emite un token de pago firmado para (uid, plan). El token NO activa el plan
    por sí mismo: debe presentarse a verify_payment_token()/confirm() para que
    el backend actualice el estado de la suscripción.

    Raises:
        ValueError  si el plan no es facturable.
    """
    if not is_billable_plan(plan):
        raise ValueError(f"Plan no facturable: {plan!r}.")

    now = datetime.datetime.now(datetime.timezone.utc)
    exp = now + datetime.timedelta(seconds=TOKEN_TTL_SECONDS)
    amount = compute_amount(plan)
    # Identificador de transacción legible para el recibo (no secreto).
    txn = "BD-" + secrets.token_hex(4).upper() + "-" + str(int(now.timestamp()))[-5:]

    payload = {
        "uid": uid,
        "plan": plan,
        "amount": amount,
        "txn": txn,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64url_encode(payload_bytes)
    sig = hmac.new(_signing_key(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    token = f"{_TOKEN_PREFIX}{payload_b64}.{_b64url_encode(sig)}"

    return PaymentToken(
        token=token,
        transaction_id=txn,
        uid=uid,
        plan=plan,
        amount=amount,
        issued_at=now,
        expires_at=exp,
    )


def verify_payment_token(token: str, expected_uid: str) -> PaymentToken:
    """
    Verifica la integridad y vigencia de un token de pago y que pertenezca al
    usuario `expected_uid`.

    Returns:
        PaymentToken con los datos decodificados.

    Raises:
        ValueError  si el token es inválido, está expirado, manipulado o el uid
                    no coincide. El mensaje es genérico (no filtra el motivo
                    exacto a un atacante).
    """
    if not token or not token.startswith(_TOKEN_PREFIX):
        raise ValueError("Token de pago inválido.")

    body = token[len(_TOKEN_PREFIX):]
    try:
        payload_b64, sig_b64 = body.split(".", 1)
    except ValueError:
        raise ValueError("Token de pago inválido.")

    expected_sig = hmac.new(
        _signing_key(), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        given_sig = _b64url_decode(sig_b64)
    except Exception:
        raise ValueError("Token de pago inválido.")

    # Comparación en tiempo constante: no filtra cuántos bytes coinciden.
    if not hmac.compare_digest(expected_sig, given_sig):
        raise ValueError("Token de pago inválido.")

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        raise ValueError("Token de pago inválido.")

    uid = payload.get("uid")
    plan = payload.get("plan")
    amount = payload.get("amount")
    txn = payload.get("txn")
    iat = payload.get("iat")
    exp = payload.get("exp")

    if not (uid and plan and isinstance(amount, int) and txn and iat and exp):
        raise ValueError("Token de pago inválido.")

    if uid != expected_uid:
        raise ValueError("El token de pago no pertenece a este usuario.")

    if not is_billable_plan(plan):
        raise ValueError("Token de pago inválido.")

    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    if now_ts > int(exp):
        raise ValueError("El token de pago expiró. Reinicia el pago.")

    return PaymentToken(
        token=token,
        transaction_id=txn,
        uid=uid,
        plan=plan,
        amount=int(amount),
        issued_at=datetime.datetime.fromtimestamp(int(iat), datetime.timezone.utc),
        expires_at=datetime.datetime.fromtimestamp(int(exp), datetime.timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Estado de la suscripción (derivado de los datos de Firestore)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_iso(value: object) -> datetime.datetime | None:
    """Convierte un ISO-8601 (o Firestore Timestamp/datetime) a datetime UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        try:
            dt = datetime.datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def is_expired(data: dict, now: datetime.datetime | None = None) -> bool:
    """
    True si el plan de pago del usuario ya venció. Un documento sin
    `fechaVencimiento` (p. ej. plan asignado manualmente por un admin) NUNCA
    se considera expirado — solo expiran las suscripciones con vencimiento.
    """
    venc = _parse_iso(data.get("fechaVencimiento"))
    if venc is None:
        return False
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now > venc


def effective_plan(data: dict, now: datetime.datetime | None = None) -> str:
    """
    Plan REALMENTE vigente para el usuario. Si la suscripción de pago venció,
    el plan efectivo degrada a 'gratis' aunque el campo `plan` siga en basic/pro
    (la baja efectiva ocurre al expirar el ciclo, no al cancelar).
    """
    plan = data.get("plan") or "gratis"
    if plan == "gratis":
        return "gratis"
    if is_expired(data, now):
        return "gratis"
    return plan


def build_subscription_state(data: dict) -> dict:
    """
    Construye el objeto de estado de suscripción que consume la vista de cuenta.
    Toda la veración de vigencia ocurre aquí (server-side), de modo que el
    frontend solo muestra lo que el backend considera verdadero.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    stored_plan = data.get("plan") or "gratis"
    eff_plan = effective_plan(data, now)
    expired = is_expired(data, now)

    fecha_compra = _parse_iso(data.get("fechaCompra"))
    fecha_venc = _parse_iso(data.get("fechaVencimiento"))
    renovacion = bool(data.get("renovacionAutomatica", False))

    # Estado legible:
    #   activo     → plan de pago vigente con renovación
    #   cancelado  → cancelado pero con acceso hasta el vencimiento
    #   expirado   → venció (acceso revertido a gratis)
    #   gratis     → nunca tuvo plan de pago
    if eff_plan == "gratis":
        estado = "expirado" if (stored_plan != "gratis" and expired) else "gratis"
    elif renovacion:
        estado = "activo"
    else:
        estado = "cancelado"

    return {
        "plan": eff_plan,
        "plan_almacenado": stored_plan,
        "estado": estado,
        "renovacion_automatica": renovacion,
        "fecha_compra": fecha_compra.isoformat() if fecha_compra else None,
        "fecha_vencimiento": fecha_venc.isoformat() if fecha_venc else None,
        "precio": PLAN_PRICES.get(eff_plan, {}).get("price"),
        "monto_total": compute_amount(eff_plan) if eff_plan in PLAN_PRICES else None,
        "ultimo_pago_id": data.get("ultimoPagoId"),
    }
