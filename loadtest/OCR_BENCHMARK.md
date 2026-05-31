# Benchmark de conversión OCR — CPU vs GPU (RTX 3060 Ti)

**Fecha:** 2026-05-24
**Hardware:** Ryzen 5 5600X + RTX 3060 Ti 8 GB VENTUS 3X OC LHR
**Driver NVIDIA:** 596.49 · **CUDA runtime (PyTorch):** 12.8
**Stack instalado:** `torch 2.11.0+cu128`, `torchvision 0.26.0+cu128`, `easyocr 1.7.2`
**PDF de prueba:** 10 páginas A4 @ 200 DPI, ~3 050 caracteres por página, contenido real del cache (libro "UX Strategy" de Jaime Levy).

---

## Resultado medido

| Modo | Init modelo | Total 10 pág. | Promedio/pág. | Chars OCR'd |
|---|---|---|---|---|
| **CPU** (Ryzen 5 5600X, AVX2) | 1.40 s | **181.30 s** | **18 130 ms** | 30 473 |
| **GPU** (RTX 3060 Ti, CUDA) | 1.50 s | **45.78 s** | **4 578 ms** | 30 481 |
| **Speedup** | — | **3.96×** | **3.96×** | idéntico (Δ=0.03 %) |

Los caracteres extraídos son prácticamente idénticos (diferencia <0.03 %, dentro del ruido de detección sub-píxel). Ambos modos hicieron exactamente el mismo trabajo de OCR — la única variable fue el hardware.

---

## Antes vs ahora: ¿qué cambió realmente?

| Tipo de PDF | Antes (sin torch) | Ahora (con CUDA) |
|---|---|---|
| **Digital nativo** (texto incrustado: ePub→PDF, papers arXiv, libros Calibre) | PyMuPDF — ~50-200 ms/página | **Idéntico**. PyMuPDF nativo en C es más rápido que GPU. La GPU **no entra**. |
| **Escaneado** (>40 % páginas sin capa texto) | Error: "Sin texto extraíble. Activa OCR…" | **EasyOCR + CUDA, ~4.6 s/página**. Funcionando. |
| **Mixto** (algunas páginas escaneadas) | Solo se extraía la parte digital | Páginas escaneadas pasan por OCR GPU automáticamente |

### Traducción a tiempos de libros completos

| Páginas | CPU (antes posible si instalabas easyocr CPU) | GPU (ahora) |
|---|---|---|
| 50 págs | ~15 min | **~3.8 min** |
| 100 págs | ~30 min | **~7.6 min** |
| 300 págs (libro grueso) | ~91 min | **~23 min** |
| 1 000 págs (manual técnico) | ~5 h | **~1 h 16 min** |

---

## Footprint en VRAM y concurrencia

```
Modelo EasyOCR cargado (es+en): 1.17 GB VRAM
GPU total disponible: 8.00 GB
Margen libre: 6.83 GB
```

El semáforo en `pdf_engine.py:85` limita a **2 OCR concurrentes** = ~2.34 GB VRAM. Margen de 5.66 GB para cargas mayores o para ejecutar otras aplicaciones (compositor de Windows, navegador, gaming) sin chocar.

**Concurrencia efectiva con HTTP_WORKERS=2 (multi-worker):**
- Cada HTTP worker tiene su propio `_gpu_sem(2)` → potencialmente 4 OCR concurrentes en VRAM.
- 4 × 1.17 GB = 4.68 GB. **Aún seguro**.
- Con `HTTP_WORKERS≥4` habría que bajar el semáforo a 1 (o pasar a un semáforo IPC compartido).

---

## Caveats honestos

1. **Sólo afecta OCR.** Para PDFs digitales nativos (90 % de bibliotecas en BookDork hoy) la GPU **no cambia nada**. El cuello de botella en esos casos es disco y MarkItDown, no compute.

2. **3.96× ≠ 10-15× de literatura.** Las cifras de papers (EasyOCR/PaddleOCR) suelen citarse en GPUs de gama alta (4090, A100) y con batches grandes. La RTX 3060 Ti es gama media y EasyOCR procesa página-por-página sin batching agresivo. El factor 4× es lo realista en este hardware.

3. **Cold start del modelo:** ~1.5 s la primera vez que se carga (`_get_ocr_reader` es lazy). Subsecuentes conversiones del mismo proceso reutilizan el reader. Si el server se reinicia, el primer OCR paga ese coste.

4. **Primera descarga del modelo:** ~700 MB de pesos a `~/.easyocr/`. Una sola vez por usuario.

5. **Tamaño en disco del stack:** ~5-6 GB (torch+CUDA wheels son grandes; `torch_cuda.dll` solo pesa 774 MB).

6. **Python 3.14 + Windows quirk:** los wheels `cu128` necesitan `torch/lib` en DLL search path. En la mayoría de instalaciones funciona vainilla; si alguna vez aparece `OSError: caffe2_nvrtc.dll`, el fix es añadir `os.add_dll_directory(<torch>/lib)` antes del `import torch` en `pdf_engine.py`. Hoy no se observó tras la instalación.

7. **HTTP serving no se beneficia.** El techo de 285 RPS del load test sigue intacto: las páginas, búsquedas y health checks no hacen OCR. La GPU mejora **solo** el endpoint `/api/convert` cuando recibe un PDF escaneado.

---

## Estado actual del sistema

```
✓ torch          2.11.0+cu128  (importable, CUDA detectada)
✓ torchvision    0.26.0+cu128
✓ easyocr        1.7.2
✓ GPU            NVIDIA GeForce RTX 3060 Ti — 8 GB VRAM
✓ pdf_engine.get_engine_status() → {'pymupdf': True, 'cuda': True, 'markitdown': True, 'ocr_ready': False}
   (ocr_ready=False es esperado: el modelo se carga lazy en la primera conversión)
```

El servidor BookDork (dev en :8000) ya detecta CUDA. **No requiere cambios de código** — el path GPU estaba diseñado y solo le faltaban las dependencias.

---

## Reproducibilidad

```bash
# 1. Generar PDF "escaneado" controlado (10 páginas, texto real del cache)
python loadtest/gen_scanned_pdf.py --pages 10

# 2. Correr benchmark CPU vs GPU
python loadtest/benchmark_ocr.py --pages 10 --dpi 200

# Salida JSON: loadtest/results_ocr_bench.json
```

El generador usa PIL+TTF (Arial) para garantizar unicode válido. El benchmark mide ambos modos sobre las mismas arrays en RAM, en orden CPU→GPU (no afecta la medición porque ambos usan I/O ya cargado en RAM).
