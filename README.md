<div align="center">

# 📚 BookDork

**Millones de libros. Un formato que tu IA entiende.**

Motor de búsqueda de libros digitales y convertidor a Markdown optimizado para IA:
encuentra libros en fuentes abiertas y reduce **hasta un 80 % los tokens** al pasarlos a
ChatGPT, Claude, Gemini o Llama.

![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![Meilisearch](https://img.shields.io/badge/Meilisearch-1.x-FF5CAA?logo=meilisearch&logoColor=white)
![HTTP/3](https://img.shields.io/badge/HTTP%2F3-QUIC-blueviolet)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)

</div>

---

## Tabla de contenidos

- [¿Qué es BookDork?](#qué-es-bookdork)
- [Características](#características)
- [Arquitectura](#arquitectura)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Requisitos](#requisitos)
- [Puesta en marcha (desarrollo)](#puesta-en-marcha-desarrollo)
- [Despliegue en producción (Docker)](#despliegue-en-producción-docker)
- [Variables de entorno](#variables-de-entorno)
- [API](#api)
- [Tests](#tests)
- [Seguridad](#seguridad)
- [Licencia](#licencia)

---

## ¿Qué es BookDork?

BookDork es una plataforma web que combina dos herramientas en un mismo flujo:

1. **Buscador de libros digitales** — localiza obras (PDF, EPUB, MOBI, DJVU) en fuentes
   abiertas (Internet Archive, Project Gutenberg, Open Library, OAPEN…) combinando un
   índice local **Meilisearch** (búsqueda con tolerancia a errores tipográficos) con un
   motor de **Google Dorks** que construye consultas avanzadas.
2. **Convertidor a Markdown** — transforma libros a un Markdown limpio, optimizado para
   modelos de lenguaje, reduciendo drásticamente el consumo de tokens. Incluye motor STEM
   de alta fidelidad (recuperación de glifos, notación matemática LaTeX) y **OCR opcional
   por GPU** para PDFs escaneados.

Sobre esto se montan **The Info Vault** (biblioteca de libros ya convertidos, por plan) y
un sistema de **autenticación y planes** vía Firebase.

---

## Características

- 🔎 **Búsqueda híbrida**: índice Meilisearch + generación de Google Dorks (`filetype:`,
  `site:`, `intitle:`, `intext:`, `inurl:`, frases exactas, `index of`…).
- 🤖 **Conversión optimizada para IA**: PDF/EPUB/MOBI/AZW3/DJVU/TXT → Markdown, hasta 5
  archivos en paralelo, con caché en disco para evitar reconversiones.
- 🧪 **Motor STEM de alta fidelidad**: reparación de mojibake (`ftfy`), recuperación de
  nombres de glifo (`fonttools`) y parsing de LaTeX (`pylatexenc`).
- 👁️ **OCR por GPU (opcional)**: EasyOCR + CUDA para libros escaneados, con fallback a CPU.
- 🚀 **HTTP/3 (QUIC)**: servido vía Hypercorn con TLS 1.3; escala a HTTP/3 automáticamente
  vía `Alt-Svc`.
- 🔐 **Autenticación y planes**: Firebase Auth (verificación RS256 de ID tokens) + Firestore
  para límites de conversión y acceso al Vault.
- 🛡️ **Seguridad de serie**: rate limiting por IP, cabeceras de seguridad, API key admin con
  comparación en tiempo constante y search key de mínimo privilegio.

---

## Arquitectura

```
Navegador ──HTTP/3──▶ Caddy (TLS, reverse proxy) ──▶ Hypercorn / FastAPI ──▶ Meilisearch
                                                            │
                                                            ├─▶ Firebase Auth + Firestore
                                                            └─▶ Conversión (PyMuPDF / MarkItDown / OCR)
```

| Capa            | Tecnología                                            |
|-----------------|-------------------------------------------------------|
| Frontend        | HTML + CSS + JavaScript vanilla (servido por FastAPI) |
| API / backend   | FastAPI 0.115 · Pydantic 2                            |
| Servidor ASGI   | Hypercorn `[h3]` · aioquic (HTTP/1.1, HTTP/2, HTTP/3) |
| Búsqueda        | Meilisearch 1.x                                       |
| Auth / datos    | Firebase Admin SDK · Firestore                        |
| Conversión      | PyMuPDF · MarkItDown · EasyOCR (opcional)            |
| Proxy / TLS prod| Caddy 2                                               |
| Orquestación    | Docker · Docker Compose                               |

---

## Estructura del proyecto

```
.
├── backend/                  # Aplicación FastAPI (paquete Python)
│   ├── main.py               # App, rutas, lifespan, pools de conversión
│   ├── config.py             # Settings (pydantic-settings, carga .env)
│   ├── security.py           # Rate limit, cabeceras, require_admin_key
│   ├── meilisearch_client.py # Cliente Meilisearch (search key + master key)
│   ├── dork_engine.py        # Motor de Google Dorks
│   ├── pdf_engine.py         # Conversión a Markdown + OCR
│   ├── stem_engine.py        # Motor STEM (glifos, LaTeX, mojibake)
│   ├── conversion_cache.py   # Caché SQLite de conversiones (Vault)
│   ├── firebase_auth.py      # Verificación de ID tokens, planes
│   ├── firebase_admin_client.py
│   ├── admin_routes.py       # Endpoints /api/admin/*
│   └── models.py             # Modelos Pydantic de request/response
├── Frontend/                 # SPA estática (landing, search, converter, vault, auth, plans)
├── tests/                    # Tests y benchmarks (pytest)
├── loadtest/                 # Scripts de pruebas de carga
├── start.py                  # Arranque orquestado (Meilisearch + servidor)
├── gen_certs.py              # Genera certificado TLS local con mkcert
├── build_assets.py           # Build de assets del frontend
├── requirements.txt
├── Dockerfile                # Imagen del backend (multi-stage, python:3.12-slim)
├── docker-compose.prod.yml   # Stack de producción (meili + backend + caddy)
├── Caddyfile                 # Configuración del reverse proxy
└── .env.example              # Plantilla de configuración (copiar a .env)
```

---

## Requisitos

- **Python 3.12+** (la imagen Docker usa `python:3.12-slim`; `aioquic` requiere ≥ 3.10).
- **Docker Desktop** — para Meilisearch en local y para el despliegue de producción.
- **[mkcert](https://github.com/FiloSottile/mkcert)** — para certificados TLS de confianza
  en local (HTTP/3 sin advertencias).
- **(Opcional) GPU NVIDIA + CUDA** — para acelerar el OCR de PDFs escaneados.

---

## Puesta en marcha (desarrollo)

### 1. Clonar e instalar dependencias

```bash
git clone <url-del-repo> bookdork
cd bookdork

python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS / Linux:
# source .venv/bin/activate

pip install -r requirements.txt
```

> El OCR por GPU (`easyocr`, `torch`, `torchvision`) viene comentado en `requirements.txt`.
> Descoméntalo e instálalo manualmente solo si quieres conversión de PDFs escaneados.

### 2. Configurar variables de entorno

```bash
cp .env.example .env           # PowerShell: Copy-Item .env.example .env
```

Genera claves fuertes y colócalas en `.env`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # MEILI_MASTER_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"   # ADMIN_API_KEY
```

> **Nunca** subas el `.env` real ni la carpeta `certs/` al repositorio o a la nube.
> El `.gitignore` ya ignora `.env`, `certs/`, `*.key` y `*.crt`.

### 3. Generar certificados TLS (HTTP/3)

```bash
python gen_certs.py            # requiere mkcert instalado
```

### 4. Arrancar

`start.py` levanta Meilisearch en Docker (lo crea/recrea si la master key no coincide),
espera a que responda y arranca el servidor.

```bash
python start.py                # producción local: Hypercorn + HTTPS + HTTP/3
python start.py --dev          # desarrollo: uvicorn HTTP con --reload
```

| Modo            | Servidor   | Protocolo            | Recarga |
|-----------------|------------|----------------------|---------|
| `python start.py`       | Hypercorn  | HTTPS + HTTP/2 + HTTP/3 | No  |
| `python start.py --dev` | uvicorn    | HTTP plano           | Sí      |

Por defecto el puerto es `HTTP3_PORT` (`.env`). Con `DEBUG=true` se expone la documentación
interactiva de la API en `/api/docs` (Swagger) y `/api/redoc`; en producción está oculta.

---

## Despliegue en producción (Docker)

El stack de producción levanta tres servicios: **Meilisearch**, **backend** y **Caddy**
(TLS automático + reverse proxy con HTTP/3).

```bash
# Variables requeridas en el entorno o en un .env junto al compose:
#   MEILI_MASTER_KEY, ADMIN_API_KEY, MEILI_SEARCH_API_KEY (opcional), APP_DOMAIN
docker compose -f docker-compose.prod.yml up -d --build
```

- La **search key de Meilisearch** debe crearse con la master key una vez el contenedor
  esté arriba (`POST /keys` con `actions:["search"]`, `indexes:["books"]`) y su valor va en
  `MEILI_SEARCH_API_KEY`. Si se deja vacía, el backend cae a la master key (funcional pero
  menos seguro).
- `TRUSTED_PROXY_DEPTH=1` en el compose hace que el rate limiter use la IP real del cliente
  (última entrada de `X-Forwarded-For`) detrás de Caddy.

Ver [`DEPLOY_SECURITY.md`](DEPLOY_SECURITY.md) para el checklist de endurecimiento.

---

## Variables de entorno

Las más relevantes (lista completa y valores por defecto en [`backend/config.py`](backend/config.py)):

| Variable                        | Por defecto                | Descripción                                                       |
|---------------------------------|----------------------------|-------------------------------------------------------------------|
| `MEILI_HOST`                    | `http://localhost:7700`    | URL del servidor Meilisearch.                                     |
| `MEILI_MASTER_KEY`              | *(insegura por defecto)*   | Master key de Meilisearch. **Obligatorio cambiar.**              |
| `MEILI_SEARCH_API_KEY`          | *(vacía)*                  | Clave de solo búsqueda (mínimo privilegio). Recomendada en prod. |
| `MEILI_INDEX_NAME`              | `books`                    | Nombre del índice.                                                |
| `ADMIN_API_KEY`                 | *(insegura por defecto)*   | Protege `/api/index/*`, `/api/cache/*`, `/api/admin/*`. **Cambiar.** |
| `DEBUG`                         | `false`                    | `true` desactiva la validación de claves de producción.          |
| `HTTP3_PORT`                    | `8443`                     | Puerto del servidor.                                              |
| `FORCE_HTTP`                    | `false`                    | `true` = HTTP plano (dev/ngrok), sin TLS.                         |
| `TLS_CERT_FILE` / `TLS_KEY_FILE`| `certs/server.crt` / `.key`| Par TLS para HTTP/3.                                              |
| `RATE_LIMIT_PER_MINUTE`         | `30`                       | Máx. peticiones por IP por minuto.                               |
| `TRUSTED_PROXY_DEPTH`           | `0`                        | Nº de proxies de confianza (1 detrás de Caddy/ngrok).            |
| `APP_DOMAIN`                    | *(vacío)*                  | Dominio de producción (añade orígenes CORS).                     |
| `FIREBASE_PROJECT_ID`           | `bookdork-b9825`           | ID del proyecto Firebase.                                         |
| `FIREBASE_SERVICE_ACCOUNT_PATH` | *(vacío)*                  | Ruta al JSON de la service account (Auth + Firestore).           |
| `HTTP_WORKERS`                  | `1`                        | Procesos HTTP (ver afinidad de CPU en `config.py`).             |

> Si `DEBUG=false` y `MEILI_MASTER_KEY` o `ADMIN_API_KEY` mantienen su valor inseguro por
> defecto, la app **se niega a arrancar** (validador en `config.py`).

---

## API

Documentación interactiva en `/api/docs` (Swagger) y `/api/redoc`, disponible solo con
`DEBUG=true`. Endpoints principales:

| Método | Ruta                              | Auth            | Descripción                                            |
|--------|-----------------------------------|-----------------|--------------------------------------------------------|
| `GET`  | `/api/health`                     | —               | Estado del sistema (apto para monitorización).         |
| `GET`  | `/api/search`                     | —               | Búsqueda de libros (Meilisearch + Dork principal).     |
| `GET`  | `/api/search/external`            | —               | Proxy server-side a Open Library (evita CORS).         |
| `GET`  | `/api/dork`                       | —               | Devuelve todas las variantes de Google Dork.           |
| `POST` | `/api/convert`                    | Firebase token  | Convierte 1–5 archivos a Markdown.                     |
| `GET`  | `/api/vault/books`                | Firebase token  | Lista libros del Vault (plan Basic/Pro).               |
| `GET`  | `/api/vault/download/{book_id}`   | Firebase token  | Descarga el Markdown de un libro del Vault.            |
| `POST` | `/api/index/book`                 | `X-Admin-API-Key` | Indexa un libro en Meilisearch.                      |
| `GET`  | `/api/index/stats`                | `X-Admin-API-Key` | Estadísticas del índice.                             |
| `POST` | `/api/cache/*`                    | `X-Admin-API-Key` | Mantenimiento del caché de conversiones.            |
| `GET`  | `/api/admin/users/by-email`       | `X-Admin-API-Key` | Datos de usuario por email.                          |
| `GET`  | `/api/admin/users/{uid}`          | `X-Admin-API-Key` | Datos de usuario por UID.                            |

Páginas servidas: `/` (landing), `/search`, `/converter`, `/vault`, `/plans`, `/auth`, `/legal`.

---

## Tests

```bash
pip install -r requirements.txt   # incluye pytest y pytest-asyncio
pytest                            # suite completa
python tests/run_all.py           # runner de tests/benchmarks STEM
```

Los recursos de `tests/` cubren principalmente el motor STEM (recuperación de glifos,
fidelidad de conversión). `loadtest/` contiene scripts de pruebas de carga.

---

## Seguridad

- **Secretos fuera del control de versiones**: `.env`, `certs/` y los JSON de credenciales
  están en `.gitignore`. Usa `.env.example` como plantilla.
- **API key admin**: comparación en tiempo constante (`secrets.compare_digest`) sobre la
  cabecera `X-Admin-API-Key`.
- **Mínimo privilegio en Meilisearch**: usa una *search key* de solo lectura para las
  búsquedas públicas; la *master key* queda reservada a indexación/administración.
- **Rate limiting y cabeceras de seguridad** aplicados por middleware.
- **Rotación de claves**: si un secreto se expone, regenera `MEILI_MASTER_KEY` y
  `ADMIN_API_KEY` en `.env`, recrea el contenedor de Meilisearch (la search key se deriva
  de la master key y debe recrearse) y regenera los certificados con `python gen_certs.py`.

---

## Licencia

Este repositorio no incluye todavía un archivo de licencia. Hasta que se añada uno, todos
los derechos quedan reservados por el autor. Añade un `LICENSE` para definir las condiciones
de uso.
