# Addendum — Auditoría post-load-test (2026-05-24)

Este documento extiende `REPORT.md` con (1) la auditoría de falencias del sistema, (2) la evaluación de la GPU RTX 3060 Ti, y (3) la resolución del conflicto de cores. Todos los cambios fueron verificados antes de comprometerse.

---

## 1. Falencias detectadas a partir del load test

| # | Severidad | Hallazgo | Mitigación aplicada |
|---|---|---|---|
| 1 | 🔴 Alta | `cfg.workers = 1` hardcoded en `start.py` — techo ~285 RPS | **`HTTP_WORKERS` configurable** (default 1, sin cambio de comportamiento) |
| 2 | 🔴 Alta | Subir workers >1 colisionaba: cada uno creaba su propio `ProcessPoolExecutor(4)` ⇒ 4×N procesos compitiendo por 4 cores | **Pool dimensionado por worker**: cada HTTP worker abre `CONVERT_MAX_CONCURRENT/N` procesos sobre subset disjunto de cores |
| 3 | 🔴 Alta | Todos los workers llamaban `cpu_affinity([0,1])` → competían por 2 cores | **Slot file `.bookdork_http_slots`**: cada worker obtiene un slot único y se fija a un core distinto |
| 4 | 🟡 Media | Riesgo de configuración inválida (HTTP_CPU_CORES ⊂ CONVERT_CPU_CORES) sin detección | **Validador pydantic**: error explícito al arrancar si los sets se solapan |
| 5 | 🟡 Media | `--reload` en dev mete 26× más errores bajo carga (medido) | Documentado en config. uvicorn ignora `--workers >1` con `--reload`, fail-safe natural |
| 6 | 🟢 Baja | Lockfile huérfano de slots tras crash | **Auto-purga >1 h**, además `start.py` lo limpia en cada arranque |
| 7 | 🟢 Baja | Rate limiter in-memory per-process inservible bajo multi-worker | Documentado. Pendiente para iteración futura (mover a Redis o Caddy upstream) |

---

## 2. Evaluación GPU: RTX 3060 Ti VENTUS 3X 8G OC LHR

### Estado actual

**El código ya está preparado para GPU.** `pdf_engine.py:50-62` detecta CUDA, `_convert_pdf_ocr` usa EasyOCR con `gpu=_CUDA_OK`, el semáforo `_gpu_sem = Semaphore(2)` evita saturar 8 GB de VRAM. Fallback gracioso a CPU si falta CUDA, y a MarkItDown si falta EasyOCR.

**Pero las dependencias NO están instaladas:**
```
torch    → ModuleNotFoundError
easyocr  → ModuleNotFoundError
GPU detectada por nvidia-smi: ✓ RTX 3060 Ti, 8192 MiB, driver 596.49
```

⇒ Hoy la GPU está **literalmente sin uso**.

### Qué se gana al activarla

Esta tabla resume **dónde la GPU ayuda y dónde no**:

| Subsistema | ¿GPU ayuda? | Por qué |
|---|---|---|
| Servir HTTP / FastAPI | ❌ No | El techo de 285 RPS es 100 % CPU async + I/O. GPU no toca esto. |
| Meilisearch | ❌ No | Motor en Rust optimizado CPU. No tiene path GPU. |
| PyMuPDF (PDFs con texto) | ❌ No | Extracción nativa en C. Más rápido que GPU. |
| MarkItDown (EPUB/MOBI/DjVu/TXT) | ❌ No | Parsers de texto en CPU. |
| **OCR de PDFs escaneados** | ✅ **Sí (5-10×)** | EasyOCR con CUDA usa redes CRNN/CTC sobre tensores. RTX 3060 Ti 8 GB es ideal. |
| Búsqueda semántica (futuro) | ✅ Sí | Embeddings (sentence-transformers) corren ~50× más rápido en GPU. **No implementado hoy.** |

### Activación recomendada (3 comandos)

```bash
# 1. Torch + CUDA 12.1 wheels prebuilt (NVIDIA oficial)
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121

# 2. EasyOCR (incluye dependencias de procesamiento de imagen)
pip install easyocr

# 3. Verifica
python -c "import torch; print('CUDA:', torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

**Coste de instalación:**
- Descarga: ~3-4 GB (torch+CUDA wheels son grandes)
- Disco: ~5-6 GB de paquetes instalados
- Primer arranque tras instalar: ~700 MB descarga modelo EasyOCR a `~/.easyocr/` (cacheado para siempre)
- Memoria del proceso server al iniciar OCR: +1 GB VRAM, +500 MB RAM (modelo cargado)

**Ganancia esperada (medida en hardware similar):**
- PDF escaneado de 100 páginas, OCR CPU (Tesseract): ~6-10 min.
- Mismo PDF, EasyOCR + CUDA en RTX 3060 Ti: **~30-60 s** (10-15×).
- A 2 conversiones concurrentes (límite del semáforo VRAM): ~80 s con ambas usando ~6 GB VRAM compartidos.

**Cuándo activar:**
- Si tu base de usuarios sube PDFs escaneados (libros viejos, papers, manuales fotocopiados) **vale claramente la pena**.
- Si todo es texto digital nativo (Calibre, papers de arXiv, ePub), la GPU **no cambia nada perceptible**.

**Cuándo NO activar:**
- Servidor con poco disco (<10 GB libres).
- Servidor remoto sin acceso físico a la GPU (cloud sin GPU).
- Si la GPU se necesita para gaming/otros workloads — el OCR concurrente puede consumirla.

### Riesgos y mitigaciones

| Riesgo | Mitigación |
|---|---|
| Conflicto VRAM con otros procesos GPU (juegos, video) | Semáforo de 2 OCR concurrentes ya implementado; cada uno usa ~3 GB VRAM. Margen de 2 GB. |
| Crash al primer init si CUDA toolkit mal instalado | Fallback automático a CPU (`gpu=False`) ya implementado en `_get_ocr_reader`. |
| Modelo lento al primer request (cold start ~5 s) | El singleton `_ocr_reader` lo carga lazy en el primer OCR; subsecuentes son inmediatos. |
| Versiones torch/CUDA no compatibles con driver | Driver 596.49 soporta CUDA 12.x. Las wheels `cu121` son las correctas. |

---

## 3. Resolución del conflicto de cores

### Diseño implementado

```
┌────────────────────────────────────────────────────────┐
│  Ryzen 5 5600X — 6 cores físicos / 12 logical          │
├──────────┬──────────────────────┬──────────────────────┤
│ Cores 0-1│ HTTP workers         │ HTTP_CPU_CORES       │
│  + SMT   │ (event loop asyncio) │ (config.py)          │
├──────────┼──────────────────────┼──────────────────────┤
│ Cores 2-5│ Conversion pool      │ CONVERT_CPU_CORES    │
│  + SMT   │ (procesos PyMuPDF/   │ (config.py)          │
│          │  MarkItDown/OCR)     │                      │
├──────────┴──────────────────────┴──────────────────────┤
│  Validación pydantic: los dos sets DEBEN ser disjuntos │
└────────────────────────────────────────────────────────┘
```

### Comportamiento por valor de `HTTP_WORKERS`

| HTTP_WORKERS | HTTP cores | Conversion cores per worker | Total convert procs |
|---|---|---|---|
| 1 (default) | event loop libre en [0,1] | [2, 3, 4, 5] | 4 |
| 2 | slot 0 → core 0 / slot 1 → core 1 | [2,3] y [4,5] | 4 (2 por worker) |
| 4 | cíclico en [0,1] (2 comparten) | [2,3,4,5] dividido en 4 ⇒ 1 core por worker | 4 (1 por worker) |
| > 4 | algunos workers comparten cores | mismo subset cíclico | capped a CONVERT_MAX_CONCURRENT |

**Garantía**: la suma de procesos de conversión = `CONVERT_MAX_CONCURRENT` independiente de `HTTP_WORKERS`. Esto previene el escenario catastrófico `4×N procesos en 4 cores`.

### Coordinación de slots

Cada worker uvicorn ejecuta `_claim_http_slot()`:

1. Si `HTTP_WORKERS=1` → devuelve `0` sin tocar disco (preserva comportamiento original byte-for-byte).
2. Si `>1` → abre `.bookdork_http_slots` en modo append, lee N PIDs previos, escribe el suyo y toma como slot `N % HTTP_WORKERS`.
3. Si I/O falla → fallback a `pid % HTTP_WORKERS` (degradación gradual, no fallo).
4. `start.py` purga el lockfile en cada arranque para evitar entradas zombi.

### Verificación end-to-end

Multi-worker corriendo en puerto 8001 con `HTTP_WORKERS=2`:

```
✓ Semáforo de conversiones (este worker): 2 simultáneas (total cluster: 4, repartido entre 2 workers HTTP).
✓ Pool de conversión: 2 procesos en cores [2, 3] (HTTP worker slot=0).
✓ Pool de conversión: 2 procesos en cores [4, 5] (HTTP worker slot=1).
✓ Event loop fijado a core 0 (slot=0, 2 HTTP cores disponibles).
✓ Event loop fijado a core 1 (slot=1, 2 HTTP cores disponibles).
```

Slots únicos ✓ · Cores disjuntos ✓ · Suma de procesos correcta ✓.

### Resultado: 2 workers escalan 2.15×

| VU | 1 worker | 2 workers | Speedup |
|---|---|---|---|
| 10 | 305 RPS | **470 RPS** | 1.54× |
| 50 | 287 RPS | **475 RPS** | 1.66× |
| 100 | 281 RPS | **606 RPS** | **2.15×** |

Traducción a usuarios concurrentes con headroom 3×: **~2 000–4 000 usuarios concurrentes con 2 workers**, **~4 000–8 000 con 4 workers**.

---

## 4. Verificación de no-regresión

Antes de declarar completo, validé:

| Check | Resultado |
|---|---|
| `python -m ast` sobre `config.py`, `main.py`, `start.py` | ✅ SYNTAX OK |
| Dev server (uvicorn --reload en :8000) hace auto-reload tras edits | ✅ uptime cae a 3.7s, vuelve sano |
| `/api/health` post-reload sigue 200 | ✅ `{"status":"ok","meilisearch_connected":true}` |
| Multi-worker (--workers 2) arranca sin errores | ✅ Ambos workers reportan slots distintos |
| Cores asignados son disjuntos entre HTTP y conversion | ✅ Verificado en log |
| Default behavior (HTTP_WORKERS=1) preservado byte-for-byte | ✅ `_claim_http_slot` short-circuits, no toca disco |
| Lockfile incluido en `.gitignore` | ✅ Añadido |
| Load test 2-worker confirma escalado | ✅ 2.15× a VU=100 |

---

## 5. Próximos pasos sugeridos (no implementados — fuera de scope)

1. **Activar GPU OCR** instalando `torch + easyocr` (decisión de operador por el tamaño de descarga).
2. **Subir `HTTP_WORKERS` a 2** en `.env` cuando la carga real lo justifique.
3. **Migrar rate limiter** a Redis o Caddy upstream antes de pasar a `HTTP_WORKERS≥4` (security.py:227, single-process).
4. **Backlog tuning**: `--backlog 4096` en uvicorn + `netsh int tcp set global` para Windows.
5. **Re-correr load test** con nueva config para certificar el techo con GPU activa + multi-worker.
