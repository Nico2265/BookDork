"""
=============================================================================
test_rbac.py — Pruebas del control de acceso basado en roles (RBAC)
=============================================================================
Cubre:
  • Jerarquía de roles y parser tolerante (resolve_role / has_min_role).
  • La dependencia require_admin_access en sus dos vías:
      - clave admin heredada (válida / inválida) → superadmin
      - identidad Firebase con custom claim 'role' (suficiente / insuficiente)
      - ausencia total de credenciales → 403

No requiere red, Firebase ni el Admin SDK: verify_firebase_token y get_settings
se sustituyen con monkeypatch. Los tests son síncronos y ejecutan las corrutinas
con asyncio.run() para no depender de la configuración de pytest-asyncio.
=============================================================================
"""

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from backend import security
from backend.firebase_auth import Role, has_min_role, resolve_role, role_level


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

class _FakeSettings:
    ADMIN_API_KEY = "test-admin-key-secret"


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.fixture(autouse=True)
def _patch_security(monkeypatch):
    """Aísla la dependencia de config y de la verificación real de tokens."""
    monkeypatch.setattr(security, "get_settings", lambda: _FakeSettings())

    async def _fake_verify(token: str) -> dict:
        # El token codifica el rol como "valid:<role>:<uid>"; cualquier otro
        # valor simula un token inválido (401), igual que verify_firebase_token.
        parts = token.split(":")
        if len(parts) == 3 and parts[0] == "valid":
            _, role, uid = parts
            return {"sub": uid, "email": f"{uid}@test.dev", "role": role}
        raise HTTPException(status_code=401, detail="Token inválido.")

    monkeypatch.setattr(security, "verify_firebase_token", _fake_verify)


def _run(dep, **kwargs):
    return asyncio.run(dep(**kwargs))


# ─────────────────────────────────────────────────────────────────────────────
# Jerarquía de roles
# ─────────────────────────────────────────────────────────────────────────────

def test_role_level_is_strictly_ordered():
    assert (
        role_level(Role.USER)
        < role_level(Role.SUPPORT)
        < role_level(Role.BILLING)
        < role_level(Role.SUPERADMIN)
    )


def test_has_min_role_inheritance():
    # superadmin satisface cualquier mínimo
    assert has_min_role(Role.SUPERADMIN, Role.BILLING)
    assert has_min_role(Role.BILLING, Role.BILLING)
    # billing no alcanza superadmin
    assert not has_min_role(Role.BILLING, Role.SUPERADMIN)
    assert not has_min_role(Role.USER, Role.SUPPORT)


@pytest.mark.parametrize(
    "value, expected",
    [
        ("support", Role.SUPPORT),
        ("SUPERADMIN", Role.SUPERADMIN),
        ("  Billing ", Role.BILLING),
        (None, Role.USER),
        ("", Role.USER),
        ("inexistente", Role.USER),   # fail-closed
        (12345, Role.USER),
        (Role.BILLING, Role.BILLING),
    ],
)
def test_resolve_role_is_fail_closed(value, expected):
    assert resolve_role(value) == expected


# ─────────────────────────────────────────────────────────────────────────────
# Vía 1: clave admin heredada
# ─────────────────────────────────────────────────────────────────────────────

def test_legacy_key_valid_grants_superadmin():
    dep = security.require_admin_access(Role.SUPERADMIN)
    principal = _run(dep, api_key="test-admin-key-secret", creds=None)
    assert principal.kind == "api_key"
    assert principal.role == Role.SUPERADMIN
    assert principal.id == "legacy-admin-key"
    assert "superadmin" in principal.audit_label


def test_legacy_key_invalid_is_denied():
    dep = security.require_admin_access(Role.SUPPORT)
    with pytest.raises(HTTPException) as exc:
        _run(dep, api_key="clave-incorrecta", creds=None)
    assert exc.value.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# Vía 2: identidad Firebase con claim de rol
# ─────────────────────────────────────────────────────────────────────────────

def test_firebase_role_sufficient():
    dep = security.require_admin_access(Role.BILLING)
    principal = _run(dep, api_key=None, creds=_bearer("valid:billing:uid12345678"))
    assert principal.kind == "firebase"
    assert principal.role == Role.BILLING
    assert principal.id == "uid12345678"
    assert principal.email == "uid12345678@test.dev"


def test_firebase_role_inherits_lower_requirement():
    # superadmin debe poder acceder a un endpoint que exige support
    dep = security.require_admin_access(Role.SUPPORT)
    principal = _run(dep, api_key=None, creds=_bearer("valid:superadmin:rootuid00"))
    assert principal.role == Role.SUPERADMIN


def test_firebase_role_insufficient_is_denied():
    dep = security.require_admin_access(Role.SUPERADMIN)
    with pytest.raises(HTTPException) as exc:
        _run(dep, api_key=None, creds=_bearer("valid:billing:uid12345678"))
    assert exc.value.status_code == 403


def test_firebase_user_without_role_is_denied():
    dep = security.require_admin_access(Role.SUPPORT)
    with pytest.raises(HTTPException) as exc:
        _run(dep, api_key=None, creds=_bearer("valid:user:plainuid01"))
    assert exc.value.status_code == 403


def test_firebase_invalid_token_propagates_401():
    dep = security.require_admin_access(Role.SUPPORT)
    with pytest.raises(HTTPException) as exc:
        _run(dep, api_key=None, creds=_bearer("garbage-token"))
    assert exc.value.status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# Sin credenciales
# ─────────────────────────────────────────────────────────────────────────────

def test_no_credentials_is_denied():
    dep = security.require_admin_access(Role.SUPPORT)
    with pytest.raises(HTTPException) as exc:
        _run(dep, api_key=None, creds=None)
    assert exc.value.status_code == 403
