# BookDork — Reporte de prueba de carga

**Fecha:** 2026-05-24
**Tester:** loadtest/run_loadtest.py (httpx + asyncio)
**Host de prueba:** mismo equipo (loopback 127.0.0.1) — Windows 11, Ryzen 5 5600X
**Backend bajo prueba:** FastAPI 1 worker (asíncrono) + Meilisearch (docker) + SQLite cache

---

## 1. Metodología

### Workload mixto (peso relativo)

| Path | Peso | Tipo |
|---|---|---|
| `/api/health` | 3.0 | endpoint ligero |
| `/search` | 2.0 | HTML servido por FileResponse |
| `/static/styles.css` | 2.0 | asset estático (devuelve 404 si está hasheado) |
| `/` | 1.0 | landing HTML |
| `/vault` | 1.0 | HTML |
| `/converter` | 0.5 | HTML |
| `/static/logo.png` | 0.5 | asset binario |

> **Decisión:** sin endpoints autenticados. La prueba mide capacidad de servir páginas y estáticos — el camino crítico de un usuario que llega al site. Los endpoints `/api/search`, `/api/vault/*`, `/api/convert` no se prueban porque requieren token Firebase y/o consultan Meilisearch/disco; para ellos haría falta una segunda batería con tokens reales.

### Stages

Concurrencia escalada: **10 → 50 → 100 → 200 → 500 → 1000** virtual users.
Cada stage corre 12 s. Warmup previo de 50 requests.

### Escenarios

1. **`dev-uvicorn-reload`** — el servidor que tenías corriendo (uvicorn `--reload`, file-watch activo).
2. **`prod-like-uvicorn`** — uvicorn sin `--reload` en puerto 8001, único cambio respecto al anterior.

### Rate limiter

Los 7 paths del workload **están exentos** del rate limiter (`security.py:229`: `/`, `/search`, `/converter`, `/auth`, `/plans`, `/api/health`, y todo `/static/*`). Por tanto el experimento "con vs sin rate limit" no aplica a este workload — los resultados reflejan capacidad bruta sin throttling.
Si quieres medir el techo bajo rate limit, hay que probar endpoints como `/legal` o `/api/vault/*` (los segundos requieren token).

---

## 2. Resultados

### Escenario A — dev (uvicorn --reload)

```
Stage         RPS   p50 ms   p95 ms   p99 ms   err%    conn-err
VU=10        292    17.4    238.8    363.6   0.0%        0
VU=50        282    97.2    212.9   1943.5   0.0%        0
VU=100       288   197.3    428.1   3766.5   0.0%        0
VU=200       141  1448.8   2191.0  10774.1  77.2%     1344     ← saturación
VU=500        60  6853.2   6966.4   6976.5  97.3%      855
VU=1000       42       —        —        —  100.0%    1003     ← colapso total
```

### Escenario B — prod-like (uvicorn sin --reload)

```
Stage         RPS   p50 ms   p95 ms   p99 ms   err%    conn-err
VU=10        305    16.6    230.5    343.4   0.0%        0
VU=50        287   119.4    219.3   1325.9   0.0%        0
VU=100       281   297.1    451.9   1560.1   0.0%        0
VU=200       129  1478.3   1832.7   5589.7   2.9%       47     ← yellow zone
VU=500        60  6798.9   6926.2   8339.8  97.2%      856
VU=1000       42      —        —       —    99.8%     1001
```

### Diferencias clave dev vs prod-like

| VU | dev err% | prod-like err% | comentario |
|---|---|---|---|
| 10–100 | 0% | 0% | comportamiento idéntico bajo carga moderada |
| **200** | **77%** | **2.9%** | el file-watch de `--reload` colapsa la capacidad de aceptar conexiones |
| 500+ | ~97% | ~97% | ambos chocan contra el mismo techo del SO (backlog/sockets) |

---

## 3. Interpretación

### Punto de saturación

- **Zona verde** — `VU ≤ 100`: 280–290 RPS sostenidos, 0% errores, p95 < 500 ms. Operación normal.
- **Zona amarilla** — `VU = 200`: RPS cae a ~130, p95 > 1.5 s, errores 3–77%. UX degradada.
- **Zona roja** — `VU ≥ 500`: throughput colapsa a ~60 RPS, 97% errores. La cola de aceptación se llena, sockets en TIME_WAIT, el server rechaza conexiones.

### Techo del backend

**~290 RPS sostenidos** es el techo de este servidor con **1 worker async**. Es consistente con un loop asyncio Python (~3-5 ms por request en path simple, capado por GIL en operaciones síncronas como `FileResponse`).

### Traducción a usuarios reales concurrentes

Un usuario activo "real" (navegando, leyendo, no scrapeando) genera entre **0.05 y 0.1 RPS** sostenidos (1 request cada 10–20 s).

Con 290 RPS de techo:
| Patrón de uso | Usuarios concurrentes activos |
|---|---|
| Browsing relajado (0.05 rps/u) | **~5 800** |
| Browsing activo (0.10 rps/u) | **~2 900** |
| Búsqueda intensa (0.20 rps/u) | **~1 450** |

> **Con headroom 3× para ráfagas** (recomendado para no rozar zona amarilla): **~1 000–2 000 usuarios concurrentes** es el rango realista soportable hoy.

Esto NO es "usuarios registrados" (pueden ser cientos de miles) — es **gente abriendo páginas al mismo tiempo**.

### Caveats

1. **Loopback ≠ red real**: el RTT añadirá 5–50 ms por request en producción. La latencia subirá, el techo de RPS bajará algo.
2. **CPU/disco del equipo de prueba**: el cliente de carga y el servidor compiten por los mismos núcleos. En un setup real (cliente y servidor separados) puede mejorar 10–20%.
3. **Workload sin API caliente**: estos números reflejan páginas estáticas y `/api/health`. El path `/api/search` (que pega a Meilisearch) tendrá ~5× más latencia y bajará el RPS techo.
4. **No probado bajo TLS / HTTP/2**: Hypercorn con HTTPS añade overhead de handshake (~50 ms en cold connection, despreciable en keep-alive).

---

## 4. Hallazgos accionables (alto ROI)

### 🔴 Crítico: aumentar el número de workers

`start.py:276` fija `cfg.workers = 1` incluso en modo prod. **Multiplicar workers escala linealmente el techo** hasta saturar núcleos. En tu CPU (6 cores físicos) podrías subir a 4-5 workers sin pelear con conversión PDF (que usa cores 2-5).

```python
# start.py — modo prod
cfg.workers = max(2, os.cpu_count() - 2)  # deja 2 cores para SO + workers de conversión
```

**Impacto estimado:** **~1 000-1 200 RPS sostenidos** = 3-4× la capacidad actual.

### 🟡 Importante: el rate limiter es in-memory y per-proceso

Si subes a multi-worker, **el rate limiter pierde efectividad** porque cada proceso tiene su propio diccionario. Un atacante recibe `N × 30` requests/min con N workers.

Solución: mover el limiter a Redis (un comando `INCR` por request) o a Caddy/Nginx upstream. Para tu escala actual (un servidor), un Caddy con `rate_limit` antes de FastAPI es la opción más barata y robusta.

### 🟡 Importante: backlog de aceptación

A VU=200 ya vemos `connection-error` y a VU=500 el server rechaza el 97%. El kernel se queda sin slots en el `accept queue`. uvicorn por defecto usa `--backlog=2048` pero el SO puede capar más bajo (Windows ~200 por defecto sin tuning).

Acción: pasar `--backlog 4096` a uvicorn y verificar `netsh int tcp show global` en Windows o `sysctl net.core.somaxconn` en Linux.

### 🟢 Menor: `--reload` impacta dramáticamente bajo carga

En la zona amarilla (VU=200) **`--reload` produce 26× más errores** que sin él. Confirma que el modo dev jamás debe quedar activo en producción ni "para testear con clientes". El switch `python start.py` (sin `--dev`) ya lo desactiva — solo hay que asegurarse de que producción no use `--dev`.

### 🟢 Menor: keep-alive y connection pooling

httpx mantiene keep-alive por defecto. Producción real (navegadores) también. Bajo HTTP/2 (Hypercorn con TLS) el multiplexado reduce el coste de conexiones — vale la pena la prueba con certificados reales.

---

## 5. Resumen ejecutivo

| Pregunta | Respuesta |
|---|---|
| **¿Cuál es el techo de RPS?** | ~285-305 RPS sostenidos en páginas estáticas, 1 worker |
| **¿Cuándo se degrada la UX?** | VU > 100 (p95 > 500 ms) |
| **¿Cuándo colapsa?** | VU ≥ 200 en dev / VU ≥ 500 en prod-like |
| **¿Cuántos usuarios concurrentes soporta hoy?** | **~1 000–2 000** con headroom (zona verde) |
| **¿Qué mueve la aguja más rápido?** | Subir `cfg.workers` de 1 a ~4 → **3-4× capacidad** |
| **¿`--reload` está OK para prod?** | **No.** 26× más errores bajo carga moderada |

---

## 6. Reproducibilidad

```bash
# Dev (servidor que ya tenías corriendo en :8000)
python loadtest/run_loadtest.py --scenario dev-uvicorn-reload --stages 10,50,100,200,500,1000 --duration 12 --output loadtest/results_dev.json

# Prod-like (levanta otra instancia sin --reload en :8001)
python -m uvicorn "backend.main:app" --port 8001 --host 127.0.0.1 --log-level warning &
python loadtest/run_loadtest.py --url http://127.0.0.1:8001 --scenario prod-like-uvicorn --stages 10,50,100,200,500,1000 --duration 12 --output loadtest/results_prod.json
```

Resultados crudos en JSON: `loadtest/results_dev.json`, `loadtest/results_prod.json`.
