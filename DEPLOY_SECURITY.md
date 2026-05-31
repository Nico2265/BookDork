# DEPLOY_SECURITY.md — Pasos manuales de hardening

Este archivo lista pasos que **no se pueden automatizar desde el código** porque
viven en consolas externas (Google Cloud, Firebase). Sin ellos, las capas
implementadas en el repo no son suficientes contra un atacante motivado.

---

## 1. Restringir el apiKey de Firebase por HTTP referrer

El `apiKey` en `Frontend/firebase.js` no es un secreto (es un identificador de
proyecto). Aun así, **debes restringirlo** para que solo funcione desde tus
dominios; de lo contrario, un atacante puede usarlo desde otro origen para
montar una réplica que confunda a tus usuarios o consuma tu cuota.

Pasos:

1. https://console.cloud.google.com/apis/credentials → proyecto `bookdork-b9825`
2. Selecciona la API key `AIzaSyDNTjRu-33BRNKo7A39Kbkk9_2U9kI1kjo`
3. **Application restrictions** → `HTTP referrers (websites)`
4. Añade:
   - `https://bookdork.<tu-dominio>/*`
   - `https://localhost/*` (solo si haces dev local con esa key)
5. **API restrictions** → `Restrict key` → marca solo:
   - `Identity Toolkit API`
   - `Token Service API`
   - `Firestore API`
6. Guardar. Tarda ~5 min en propagar.

Verificación: `curl -H "Referer: https://otro.com" https://identitytoolkit.googleapis.com/v1/...?key=AIza...` debe devolver `API_KEY_HTTP_REFERRER_BLOCKED`.

---

## 2. Activar Firebase App Check

App Check añade una prueba criptográfica de que los requests a Firestore y Auth
provienen de tu app real (no de un script con la key copiada). **Esta es la
seguridad real**; lo demás es defensa en profundidad.

Pasos:

1. https://console.firebase.google.com/project/bookdork-b9825/appcheck
2. Registra la app web con **reCAPTCHA Enterprise** (recomendado) o reCAPTCHA v3.
3. Inicialmente activa en modo **monitoreo** (no bloqueante) por 1-2 semanas.
4. Revisa la pestaña "Requests" → cuando >99% de requests legítimos aparezcan
   verificados, cambia a **enforcement** para Firestore, Auth y Storage.
5. El código ya está en `Frontend/firebase.js` — solo falta reemplazar
   `'<RECAPTCHA_ENTERPRISE_SITE_KEY>'` con la site key real de tu proyecto.
   En modo dev (placeholder sin reemplazar), App Check está desactivado
   para no romper localhost. En producción debes reemplazarlo.
   
   Obtén la site key en:
   https://console.firebase.google.com/project/bookdork-b9825/appcheck

---

## 3. Ejecutar el build antes de cada deploy

```bash
python build_assets.py
```

Genera `Frontend/dist/` con assets minificados, hasheados (`firebase.js` →
`firebase.<8hash>.js`) e imports reescritos. `main.py` sirve `dist/` si existe.

Si **no** corres el build, `main.py` cae al fallback de servir `Frontend/`
fuente (modo dev). En producción esto es subóptimo: archivos legibles, sin
cache busting determinista.

---

## 4. Headers que NO se pueden añadir desde código frontend

Ya están en `backend/security.py`. No los muevas a meta tags HTML:
algunos headers (HSTS, X-Frame-Options, COOP, CORP) son ignorados como meta.

Verificación post-deploy:
```bash
curl -sI https://bookdork.<tu-dominio>/ | grep -iE 'csp|frame|hsts|coop|corp|content-type-options'
```
Debe mostrar: `content-security-policy`, `x-frame-options: DENY`,
`strict-transport-security`, `cross-origin-opener-policy`,
`cross-origin-resource-policy`, `x-content-type-options: nosniff`.

---

## 5. Lo que NO se hizo (y por qué)

- **Anti-debugging / bloquear F12 / detectar DevTools**: bypass trivial (un
  proxy, una extensión, `--auto-open-devtools-for-tabs`). Daño colateral:
  rompe lectores de pantalla y dev legítimos. ROI negativo.
- **Mover `apiKey` a `/api/config`**: cero seguridad añadida. El endpoint sirve
  la key a quien lo pida igual. La seguridad real es el referrer-lock + App
  Check arriba.
- **Ofuscación pesada del JS (control flow flattening, string encryption)**:
  reversible en minutos con `de4js` o un LLM. Coste: errores ilegibles en
  Sentry, +30% tamaño, debugging propio costoso. Minificación sí (bandwidth).
- **Source maps**: el frontend no usa bundler, no se generan. Si en el futuro
  se añade, NO publicarlas en `dist/`.
- **SRI (Subresource Integrity) en imports ES de Firebase CDN**: los `import`
  ES module no soportan `integrity` aún en navegadores. Si esto cambia,
  añadir. Mientras tanto, la versión está pinneada (`10.14.1`).
