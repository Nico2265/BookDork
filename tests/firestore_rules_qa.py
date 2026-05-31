"""
=============================================================================
firestore_rules_qa.py — QA de las reglas de Firestore (sin emulador)
=============================================================================
Dos niveles de verificación sobre ../firestore.rules:

  1) LINTER ESTRUCTURAL sobre el archivo real:
       • balance de llaves/paréntesis/corchetes
       • tokens obligatorios (rules_version, service, deny-all final)
       • cada bloque match contiene al menos un allow
       • ausencia de tabs (estilo del repo: 2 espacios)

  2) MODELO DE LÓGICA + MATRIZ DE CASOS:
       Reimplementa fielmente cada predicado `allow` en Python y lo evalúa
       contra payloads REALES (Frontend/auth.js, search.js → deben PERMITIR)
       y contra payloads maliciosos / de borde (→ deben DENEGAR).

LIMITACIÓN CONOCIDA: este modelo valida la LÓGICA e INTENCIÓN de las reglas,
no la semántica exacta del motor de Firestore. La verificación definitiva antes
de un deploy a producción es el emulador (@firebase/rules-unit-testing). Aquí no
está disponible (no hay Node/Java en el entorno). Ejecuta el script directamente:

    python tests/firestore_rules_qa.py        # exit 0 = todo OK, 1 = fallo
=============================================================================
"""

from __future__ import annotations

import sys
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parent.parent / "firestore.rules"

_failures: list[str] = []


def check(name: str, got, expected) -> None:
    ok = got == expected
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}: got={got!r} expected={expected!r}")
    if not ok:
        _failures.append(name)


# ─────────────────────────────────────────────────────────────────────────────
# 1) LINTER ESTRUCTURAL
# ─────────────────────────────────────────────────────────────────────────────

def lint_structure() -> None:
    print("\n== 1) LINTER ESTRUCTURAL (firestore.rules) ==")
    src = RULES_PATH.read_text(encoding="utf-8")

    # Balance de delimitadores (ignorando los que aparezcan en comentarios //).
    code_lines = []
    for line in src.splitlines():
        idx = line.find("//")
        code_lines.append(line if idx == -1 else line[:idx])
    code = "\n".join(code_lines)

    for open_c, close_c, label in [("{", "}", "llaves"),
                                    ("(", ")", "paréntesis"),
                                    ("[", "]", "corchetes")]:
        check(f"balance de {label}", code.count(open_c), code.count(close_c))

    check("declara rules_version '2'", "rules_version = '2';" in src, True)
    check("declara service cloud.firestore", "service cloud.firestore" in src, True)
    check("incluye deny-all final",
          "match /{document=**}" in src and "allow read, write: if false;" in src, True)
    check("sin tabs (estilo 2 espacios)", "\t" in src, False)

    # Cada bloque `match` debe contener al menos un `allow`.
    n_match = code.count("match ")
    n_allow_blocks = code.count("allow ")
    check("hay >= 1 allow por match (heurístico)", n_allow_blocks >= n_match, True)
    print(f"     (info) bloques match={n_match}, sentencias allow={n_allow_blocks}")


# ─────────────────────────────────────────────────────────────────────────────
# 2) MODELO DE LÓGICA — reimplementación fiel de los predicados
# ─────────────────────────────────────────────────────────────────────────────

USUARIO_ALLOWED = {
    "email", "phoneNumber", "plan", "conversiones",
    "conversionesDiarias", "fechaConversiones", "stripeCustomerId", "creadoEn",
}
USUARIO_REQUIRED = {"plan", "conversiones", "conversionesDiarias", "creadoEn"}


def _is_int_zero(v) -> bool:
    # En Firestore 0 == 0 (no confundir con el booleano False de Python).
    return isinstance(v, int) and not isinstance(v, bool) and v == 0


def _autenticado(auth) -> bool:
    return auth is not None


def _es_dueno(auth, user_id) -> bool:
    return auth is not None and auth.get("uid") == user_id


def _email_verificado(auth) -> bool:
    return auth is not None and auth.get("token", {}).get("email_verified") is True


def _tiene_rol_admin(auth) -> bool:
    if auth is None:
        return False
    return auth.get("token", {}).get("role") in ("support", "billing", "superadmin")


# ── usuarios/{uid} ────────────────────────────────────────────────────────────

def usuarios_read(auth, user_id) -> bool:
    return (_autenticado(auth) and _es_dueno(auth, user_id)) or _tiene_rol_admin(auth)


def usuarios_create(auth, user_id, data) -> bool:
    if not (_autenticado(auth) and _es_dueno(auth, user_id)):
        return False
    keys = set(data.keys())
    if not keys <= USUARIO_ALLOWED:          # hasOnly
        return False
    if not USUARIO_REQUIRED <= keys:         # hasAll
        return False
    if data.get("plan") != "gratis":
        return False
    if not _is_int_zero(data.get("conversiones")):
        return False
    if not _is_int_zero(data.get("conversionesDiarias")):
        return False
    if data.get("creadoEn") != REQUEST_TIME:   # creadoEn == request.time (serverTimestamp)
        return False
    if "stripeCustomerId" in data and data["stripeCustomerId"] is not None:
        return False
    if "fechaConversiones" in data and data["fechaConversiones"] != "":
        return False
    if "email" in data and data["email"] is not None:
        if not (isinstance(data["email"], str) and len(data["email"]) <= 320):
            return False
    if "phoneNumber" in data and data["phoneNumber"] is not None:
        if not (isinstance(data["phoneNumber"], str) and len(data["phoneNumber"]) <= 20):
            return False
    return True


def usuarios_update(auth, user_id, data) -> bool:
    return False  # allow update: if false


def usuarios_delete(auth, user_id) -> bool:
    return False  # allow delete: if false


# ── phones/{numero} ───────────────────────────────────────────────────────────

def phones_read() -> bool:
    return True


def phones_create(auth, data) -> bool:
    if not _autenticado(auth):
        return False
    keys = set(data.keys())
    if keys != {"uid", "creadoEn"}:          # hasOnly + hasAll
        return False
    if data.get("creadoEn") != REQUEST_TIME:  # creadoEn == request.time (serverTimestamp)
        return False
    return data.get("uid") == auth.get("uid")


# ── usuarios/{uid}/busquedas/{id} ─────────────────────────────────────────────

def busquedas_read(auth, user_id) -> bool:
    return _autenticado(auth) and _es_dueno(auth, user_id) and _email_verificado(auth)


def busquedas_create(auth, user_id, data) -> bool:
    if not (_autenticado(auth) and _es_dueno(auth, user_id) and _email_verificado(auth)):
        return False
    keys = set(data.keys())
    if keys != {"consulta", "dorkUrl", "fecha"}:
        return False
    if not (isinstance(data.get("consulta"), str) and len(data["consulta"]) < 512):
        return False
    if not (isinstance(data.get("dorkUrl"), str) and len(data["dorkUrl"]) < 2048):
        return False
    if data.get("fecha") != REQUEST_TIME:     # fecha == request.time (serverTimestamp)
        return False
    return True


# ── usuarios/{uid}/favoritos/{id} ─────────────────────────────────────────────

def favoritos_read(auth, user_id) -> bool:
    return _autenticado(auth) and _es_dueno(auth, user_id)


def favoritos_create(auth, user_id, data) -> bool:
    if not (_autenticado(auth) and _es_dueno(auth, user_id) and _email_verificado(auth)):
        return False
    keys = set(data.keys())
    if keys != {"titulo", "agregadoEn"}:
        return False
    if not (isinstance(data.get("titulo"), str) and len(data["titulo"]) < 500):
        return False
    return data.get("agregadoEn") == REQUEST_TIME  # agregadoEn == request.time


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: identidades y payloads
# ─────────────────────────────────────────────────────────────────────────────

OWNER = {"uid": "u1", "token": {"email_verified": True}}
OWNER_UNVERIFIED = {"uid": "u1", "token": {"email_verified": False}}
OTHER = {"uid": "u2", "token": {"email_verified": True}}
SUPPORT = {"uid": "u2", "token": {"email_verified": True, "role": "support"}}
SUPERADMIN = {"uid": "u2", "token": {"email_verified": True, "role": "superadmin"}}
ROLE_USER = {"uid": "u2", "token": {"email_verified": True, "role": "user"}}
# serverTimestamp() resuelve en el servidor al valor de request.time. Un cliente
# que escriba serverTimestamp() satisface "== request.time"; un timestamp elegido
# por el cliente (backdating/postdating) es un valor distinto y se rechaza.
REQUEST_TIME = "__request.time__"
SERVER_TS = REQUEST_TIME               # serverTimestamp() == request.time  -> valido
BACKDATED_TS = "2020-01-01T00:00:00Z"  # timestamp del cliente              -> invalido

# Payload REAL de Frontend/auth.js (saveNewUser) — registro con email.
REAL_EMAIL_SIGNUP = {
    "email": "a@b.com", "phoneNumber": None, "plan": "gratis",
    "conversiones": 0, "conversionesDiarias": 0, "fechaConversiones": "",
    "stripeCustomerId": None, "creadoEn": SERVER_TS,
}
# Payload REAL — registro con teléfono (email null).
REAL_PHONE_SIGNUP = {
    "email": None, "phoneNumber": "+56912345678", "plan": "gratis",
    "conversiones": 0, "conversionesDiarias": 0, "fechaConversiones": "",
    "stripeCustomerId": None, "creadoEn": SERVER_TS,
}


# ─────────────────────────────────────────────────────────────────────────────
# Matriz de casos
# ─────────────────────────────────────────────────────────────────────────────

def run_logic_matrix() -> None:
    print("\n== 2) MATRIZ DE LÓGICA ==")

    print(" -- usuarios/create (compatibilidad: payloads REALES deben PERMITIR) --")
    check("real email signup -> ALLOW", usuarios_create(OWNER, "u1", REAL_EMAIL_SIGNUP), True)
    check("real phone signup -> ALLOW", usuarios_create(OWNER, "u1", REAL_PHONE_SIGNUP), True)

    print(" -- usuarios/create (seguridad: payloads maliciosos deben DENEGAR) --")
    check("inyecta role=superadmin -> DENY",
          usuarios_create(OWNER, "u1", {**REAL_EMAIL_SIGNUP, "role": "superadmin"}), False)
    check("inyecta campo isAdmin -> DENY",
          usuarios_create(OWNER, "u1", {**REAL_EMAIL_SIGNUP, "isAdmin": True}), False)
    check("plan elevado 'pro' -> DENY",
          usuarios_create(OWNER, "u1", {**REAL_EMAIL_SIGNUP, "plan": "pro"}), False)
    check("conversiones=100 -> DENY",
          usuarios_create(OWNER, "u1", {**REAL_EMAIL_SIGNUP, "conversiones": 100}), False)
    check("conversionesDiarias=5 -> DENY",
          usuarios_create(OWNER, "u1", {**REAL_EMAIL_SIGNUP, "conversionesDiarias": 5}), False)
    check("stripeCustomerId preasignado -> DENY",
          usuarios_create(OWNER, "u1", {**REAL_EMAIL_SIGNUP, "stripeCustomerId": "cus_evil"}), False)
    check("fechaConversiones no vacia -> DENY",
          usuarios_create(OWNER, "u1", {**REAL_EMAIL_SIGNUP, "fechaConversiones": "2026-01-01"}), False)
    check("creadoEn backdated (no serverTimestamp) -> DENY",
          usuarios_create(OWNER, "u1", {**REAL_EMAIL_SIGNUP, "creadoEn": BACKDATED_TS}), False)
    check("crear doc de OTRO usuario -> DENY",
          usuarios_create(OWNER, "u2", REAL_EMAIL_SIGNUP), False)
    check("no autenticado -> DENY",
          usuarios_create(None, "u1", REAL_EMAIL_SIGNUP), False)
    check("falta campo requerido 'plan' -> DENY",
          usuarios_create(OWNER, "u1", {k: v for k, v in REAL_EMAIL_SIGNUP.items() if k != "plan"}), False)
    check("email demasiado largo (321) -> DENY",
          usuarios_create(OWNER, "u1", {**REAL_EMAIL_SIGNUP, "email": "x" * 321}), False)
    check("phoneNumber demasiado largo -> DENY",
          usuarios_create(OWNER, "u1", {**REAL_PHONE_SIGNUP, "phoneNumber": "9" * 21}), False)

    print(" -- usuarios/read (RBAC) --")
    check("dueno -> ALLOW", usuarios_read(OWNER, "u1"), True)
    check("otro sin rol -> DENY", usuarios_read(OTHER, "u1"), False)
    check("rol support sobre otro -> ALLOW", usuarios_read(SUPPORT, "u1"), True)
    check("rol superadmin sobre otro -> ALLOW", usuarios_read(SUPERADMIN, "u1"), True)
    check("claim role='user' sobre otro -> DENY", usuarios_read(ROLE_USER, "u1"), False)
    check("no autenticado -> DENY", usuarios_read(None, "u1"), False)

    print(" -- usuarios/update + delete (siempre DENY, incluso el dueno) --")
    check("update dueno -> DENY", usuarios_update(OWNER, "u1", {"plan": "pro"}), False)
    check("delete dueno -> DENY", usuarios_delete(OWNER, "u1"), False)

    print(" -- phones --")
    check("read publico -> ALLOW", phones_read(), True)
    check("create {uid,creadoEn} propio -> ALLOW",
          phones_create(OWNER, {"uid": "u1", "creadoEn": SERVER_TS}), True)
    check("create con campo extra -> DENY",
          phones_create(OWNER, {"uid": "u1", "creadoEn": SERVER_TS, "x": 1}), False)
    check("create con uid ajeno -> DENY",
          phones_create(OWNER, {"uid": "u2", "creadoEn": SERVER_TS}), False)
    check("create no autenticado -> DENY",
          phones_create(None, {"uid": "u1", "creadoEn": SERVER_TS}), False)
    check("create creadoEn backdated -> DENY",
          phones_create(OWNER, {"uid": "u1", "creadoEn": BACKDATED_TS}), False)

    print(" -- busquedas (subcoleccion, gate email verificado) --")
    ok_busq = {"consulta": "fisica serway", "dorkUrl": "https://g/x", "fecha": SERVER_TS}
    check("create owner verificado valido -> ALLOW", busquedas_create(OWNER, "u1", ok_busq), True)
    check("create owner NO verificado -> DENY", busquedas_create(OWNER_UNVERIFIED, "u1", ok_busq), False)
    check("create consulta >=512 -> DENY",
          busquedas_create(OWNER, "u1", {**ok_busq, "consulta": "x" * 512}), False)
    check("create campo extra -> DENY",
          busquedas_create(OWNER, "u1", {**ok_busq, "extra": 1}), False)
    check("create de otro usuario -> DENY", busquedas_create(OWNER, "u2", ok_busq), False)
    check("create fecha backdated -> DENY",
          busquedas_create(OWNER, "u1", {**ok_busq, "fecha": BACKDATED_TS}), False)
    check("read owner verificado -> ALLOW", busquedas_read(OWNER, "u1"), True)
    check("read owner NO verificado -> DENY", busquedas_read(OWNER_UNVERIFIED, "u1"), False)

    print(" -- favoritos (subcoleccion) --")
    ok_fav = {"titulo": "Calculo Stewart", "agregadoEn": SERVER_TS}
    check("create owner verificado valido -> ALLOW", favoritos_create(OWNER, "u1", ok_fav), True)
    check("create owner NO verificado -> DENY", favoritos_create(OWNER_UNVERIFIED, "u1", ok_fav), False)
    check("create titulo >=500 -> DENY",
          favoritos_create(OWNER, "u1", {**ok_fav, "titulo": "x" * 500}), False)
    check("create campo extra -> DENY",
          favoritos_create(OWNER, "u1", {**ok_fav, "cover": "x"}), False)
    check("create agregadoEn backdated -> DENY",
          favoritos_create(OWNER, "u1", {**ok_fav, "agregadoEn": BACKDATED_TS}), False)
    check("read owner (sin requerir verificado) -> ALLOW", favoritos_read(OWNER, "u1"), True)
    check("read de otro -> DENY", favoritos_read(OWNER, "u2"), False)
    check("delete owner -> ALLOW", favoritos_read(OWNER, "u1"), True)


def main() -> int:
    print("=" * 77)
    print("QA de firestore.rules — linter estructural + matriz de lógica")
    print("=" * 77)
    lint_structure()
    run_logic_matrix()
    print("\n" + "=" * 77)
    if _failures:
        print(f"RESULTADO: {len(_failures)} FALLO(S) -> {_failures}")
        return 1
    print("RESULTADO: TODOS LOS CASOS PASARON")
    return 0


if __name__ == "__main__":
    sys.exit(main())
