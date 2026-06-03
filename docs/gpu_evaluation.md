# Evaluación: ¿conviene que el convertidor funcione "solo con GPU"?

> Pregunta del negocio: *"¿Es mejor que el convertidor funcione solamente con
> la tarjeta gráfica para evitar el solapamiento CPU/GPU y mejorar la velocidad,
> priorizando la calidad de la información?"*

**Veredicto corto: no.** Forzar *toda* la conversión a la GPU no es posible ni
beneficioso. El "solapamiento" CPU/GPU que existe hoy es **deliberado y mejora la
velocidad**, no la perjudica. A continuación el detalle técnico y lo que sí
implementamos para cumplir el objetivo (velocidad + calidad).

---

## 1. Dónde ocurre realmente el trabajo

El pipeline tiene tres rutas (`backend/pdf_engine.py`):

| Ruta | Disparador | Hardware | ¿GPU aplica? |
|------|-----------|----------|--------------|
| **PyMuPDF** | PDF con texto incrustado (caso común) | CPU (C puro) | ❌ No existe parser de PDF en GPU |
| **STEM** | PDF con densidad matemática ≥ 5% | CPU | ❌ Regex/Unicode/heurística |
| **EasyOCR** | PDF escaneado (páginas-imagen) | **GPU (CUDA)** | ✅ Inferencia de red neuronal |
| **MarkItDown** | EPUB / MOBI / AZW3 / DjVu / TXT | CPU | ❌ Parsing de formato |

**La GPU solo interviene en el OCR de PDFs escaneados.** Extraer texto de un PDF
digital o parsear un EPUB es *parsing*, no inferencia: no hay equivalente en GPU,
y forzarlo sería imposible (no existe un "PDF parser CUDA") o más lento.

Para un PDF digital de 1 MB, PyMuPDF extrae el texto en **<1 s en CPU**, 10–50×
más rápido que cualquier alternativa. Pasarlo por GPU no aplica.

## 2. El "solapamiento" CPU/GPU es la optimización, no el problema

En el OCR (`_convert_pdf_ocr`), el diseño actual es correcto y de alto
rendimiento:

```
Worker 1: [render pág 1 en CPU] → [OCR pág 1 en GPU]
Worker 2:        [render pág 2 en CPU] → [OCR pág 2 en GPU]
Worker 3:               [render pág 3 en CPU] → ...
```

Mientras un worker espera el slot de GPU, otro rasteriza su página en CPU. Ese
**overlap mantiene la GPU alimentada** y maximiza el throughput. Eliminarlo para
ir "solo GPU" (rasterizar y luego inferir en serie) **dejaría la GPU ociosa**
entre páginas y haría el OCR *más lento*, no más rápido.

La contención que sí importa es **CPU vs CPU** (event loop HTTP compitiendo con
los procesos de conversión), y ya está resuelta con afinidad de núcleos
(`HTTP_CPU_CORES` disjunto de `CONVERT_CPU_CORES`).

## 3. Lo que sí hicimos para velocidad + calidad

El objetivo (<5 s, sin sacrificar calidad) se cumple **eliminando overhead**, no
degradando la extracción:

1. **Fetch de metadatos ISBN fuera de la ruta crítica.** Era una llamada HTTP
   externa (hasta varios segundos) que el usuario esperaba *después* de tener el
   Markdown listo. Ahora se sirve aparte (`/api/book-meta`, carga diferida) y la
   portada se enriquece en background. → mayor ahorro de latencia.
2. **Imports pesados diferidos** (`torch`/`easyocr`/`markitdown`). Los procesos
   del pool que solo hacen PDF digital ya no cargan CUDA en el spawn.
3. **Pre-warm del ProcessPool**: los workers se crean al arrancar, no en la
   primera conversión.
4. **Persistencia y clasificación fuera del event loop** (`asyncio.to_thread`).
5. **Instrumentación de timing por etapa** para verificar con datos
   (`read / convert / persist / total`).

Ninguno de estos toca la calidad de la extracción: el motor STEM de alta
fidelidad sigue activo y el OCR no baja de DPI.

## 4. Concesión a la petición: modo `OCR_FORCE_GPU`

Para quien quiera OCR **puramente en GPU** (resultados deterministas y de máxima
fidelidad, sin que el modelo CPU divergente intervenga en bordes), se añadió el
flag de configuración:

```env
OCR_FORCE_GPU=true
```

Cuando está activo, el OCR de páginas escaneadas **nunca cae a CPU**: espera el
slot de GPU de forma bloqueante. Si no hay CUDA disponible, no se hace OCR en vez
de degradar a CPU. Esto honra la intención de "solo GPU" **donde la GPU sí aplica
(OCR)**, mientras la ruta rápida de PDFs digitales sigue en CPU, que es lo
óptimo.

**Trade-off:** bajo saturación de GPU, `OCR_FORCE_GPU=true` aumenta la latencia
(cola en vez de fallback). Recomendado solo si la prioridad absoluta es la
fidelidad del OCR y el volumen de PDFs escaneados es bajo. Por defecto: `false`.

## 5. Recomendación final

- **No** hagas el convertidor "solo GPU" de forma global: romperías la ruta más
  rápida (PyMuPDF en CPU) sin ganancia posible.
- Mantén el **overlap CPU/GPU en OCR**: es lo que da velocidad ahí.
- Si priorizas fidelidad de OCR, activa `OCR_FORCE_GPU=true` y, opcionalmente,
  sube `OCR_DPI` a 300.
- La meta de <5 s se logra con las optimizaciones de overhead de la sección 3,
  verificables en el log `timing(s): read=… convert=… persist=… total=…`.
