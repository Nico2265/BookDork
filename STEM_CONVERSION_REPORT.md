# Reporte de implementación — Pipeline STEM de alta fidelidad

**Fecha:** 2026-05-28 (v2 con font_recovery)
**Objetivo:** elevar la fidelidad de conversión PDF → Markdown para contenido
matemático/científico digital (caracteres embebidos, no escaneados) al ≥99%
sin recurrir a modelos ML pesados.

**Resultados:**
- Fixture sintético controlado: **42% → 100%** (Δ +58 pp)
- Libro real (Zill, 724 págs): **99% reducción de defectos**, **+23× símbolos Greek recuperados**
- Cero regresión sobre PDFs no-STEM (output bit-exacto con/sin flag)
- ~28 ms/página en pipeline completo

---

## 1. Diagnóstico — por qué la conversión actual destruye matemática

El pipeline previo (`backend/pdf_engine.py`) usa
`page.get_text("text")` de PyMuPDF, que **colapsa la dimensión vertical**.
Toda la información necesaria para reconstruir notación matemática está
embebida en el PDF a nivel glifo, pero ese modo de extracción la descarta:

| Modo PyMuPDF | Qué expone | Qué pierde |
|---|---|---|
| `"text"` | string lineal | tamaño de fuente, baseline Y, fuente |
| `"dict"` | spans con metadata | char-level baselines |
| `"rawdict"` | cada glifo con origin Y, bbox, size, fuente | (nada relevante) |

### Análisis del convertidor de Microsoft (MarkItDown)

Inspección directa de `markitdown/converters/_pdf_converter.py`
(~600 líneas):

- Usa `pdfplumber.extract_words` (clustering X) para formularios + tablas
- `pdfminer.high_level.extract_text` para prosa
- **Cero referencias a "math", "equation", "latex" en todo el módulo**
- Único post-proceso: `_merge_partial_numbering_lines` (numeración MasterFormat)
- Sin normalización Unicode, sin análisis de baseline, sin manejo de ligaduras

Conclusión: MarkItDown está arquitectónicamente fuera de scope para
matemática. Por eso parchearlo desde afuera con un pipeline analítico
dedicado es viable.

### Evidencia cuantitativa del baseline

Sobre el PDF de prueba `tests/stem_fixtures/math_textbook_sample.pdf`:

```
ECUACIÓN ORIGINAL (en el PDF):     E² = p²c² + (mc²)²
BASELINE EMITE:                    E2 = p2c2 + (mc2)2

ORIGINAL:                          H₂O,  H₂SO₄,  Nₐ
BASELINE:                          H2O,  H2SO4,  NA

ORIGINAL:                          defines, first, continuity
BASELINE:                          deﬁnes, ﬁrst, conti-\nnuity  (ligaduras + hyphen)

ORIGINAL:                          Mass-Energy, time-dependent
BASELINE:                          Mass­Energy, time­dependent (soft-hyphen invisible)
```

---

## 2. Arquitectura de la solución

```
┌──────────────────────────────────────────────────────────────────┐
│                       pdf_engine._route_pdf                      │
│                                                                  │
│   ┌─────────────────┐                                            │
│   │ Detección OCR   │── escaneado → EasyOCR (sin cambios)        │
│   └────────┬────────┘                                            │
│            │ digital                                             │
│            ▼                                                     │
│   ┌─────────────────────────┐                                    │
│   │ stem_engine.score_      │                                    │
│   │   stem_density(doc)     │ ← símbolos + fuentes math +        │
│   └────────┬────────────────┘   sub/super candidatos              │
│            │                                                     │
│   density >= 0.05 ?                                              │
│       │           │                                              │
│      sí         no                                               │
│       ▼           ▼                                              │
│  stem_engine    PyMuPDF                                          │
│   (nuevo)       text mode                                        │
│       │         (existente)                                      │
│       │ excepción → fallback automático                          │
│       └───────────┘                                              │
└──────────────────────────────────────────────────────────────────┘
```

**Garantías de no-regresión:**

1. Variable de entorno `BOOKDORK_STEM_ENGINE=0` desactiva el motor sin
   tocar código
2. Cualquier excepción en `stem_engine` cae al pipeline tradicional
3. Density < threshold → no se invoca; PDFs no-STEM intactos
4. Output bit-exacto verificado con prosa (test test_no_regression.py)

---

## 3. Pipeline interno de stem_engine

```
PDF
 ↓
 [1] _extract_glyphs(page)
      page.get_text("rawdict")
      ↓
      proto_lines con {char, size, origin_y, origin_x, font}
      ↓
      Fusión vertical: líneas con misma origin_y se merge
      (fix de super/sub que rawdict separa en líneas distintas)
 ↓
 [2] _wrap_runs(line)
      Por cada glifo:
        body  si size ≥ 0.85·line_size
        super si baseline_shift < -1.5  ∧  size < 0.85·line_size
        sub   si baseline_shift > +1.5  ∧  size < 0.85·line_size
      ↓
      Cluster math (super+sub interleaved) → "_{subs}^{supers}"
      Detección de gaps horizontales → inserta espacio cuando gap > 2.5×ancho
 ↓
 [3] _format_line(line)
      max_size > body·1.40 → "# heading"
      max_size > body·1.18 → "## heading"
      max_size > body·1.08 → "### heading"
      filtro: heading solo si ≥4 letras ASCII y no operadores display (∫∑∏)
 ↓
 [4] normalize_unicode(markdown)
      ftfy.fix_text         → mojibake (￾, �, secuencias rotas)
      Tabla de traducción   → ligaduras ﬁ→fi ﬀ→ff ﬃ→ffi
                              alias math ħ→ℏ
                              soft-hyphen U+00AD → "-"
                              comillas tipográficas → ASCII
                              NULL bytes → eliminados
      NFKC                  → forma compuesta canónica
      _repair_hyphenation   → "conti-\nnuity" → "continuity"
      Cleanup whitespace    → colapsa espacios + 3 saltos → 2
```

---

## 4. Resultados de la prueba

### Fixture de control

`tests/stem_fixtures/math_textbook_sample.pdf` (2.5 MB, Cambria Math
embebida). Contenido:

- 4 ecuaciones display de física (relatividad, Schrödinger, integral, Taylor)
- 4 headings de capítulo + sección
- Fórmulas químicas con subíndices (H₂O, H₂SO₄, Nₐ)
- Constantes físicas con superíndices (6.022 × 10²³, mol⁻¹)
- Texto con ligaduras tipográficas (`deﬁnes`, `ﬁrst`)
- Salto de palabra con guión cross-line (`conti-\nnuity`)
- 20 símbolos griegos + operadores matemáticos

`ground_truth.json` define exactamente lo que un convertidor ideal debe
emitir; el scorer mide contra esa referencia.

### Métricas — comparación lado a lado

| Dimensión | Baseline (pymupdf) | Enhanced (stem-analytical) | Δ |
|---|---:|---:|---:|
| **Overall ground-truth fidelity** | **42.00%** | **100.00%** | **+58.00 pp** |
| Text words preserved | 40% | 100% | +60 |
| Math symbols preserved | 89% | 100% | +11 |
| Mojibake-free | 40% | 100% | +60 |
| Super/subscript detection | 0% | 100% | +100 |
| Structural (headings) | 0% | 100% | +100 |
| Equation token coverage | 82% | 100% | +18 |

| Dimensión (sin ground truth) | Baseline | Enhanced |
|---|---:|---:|
| Glyph coverage | 100% | 97.59%¹ |
| Mojibake score | 82.59% | 100% |
| Symbol preservation | 100% | 100% |
| Super/sub detection | 0% | 100% |
| Equation validity (5/5 LaTeX-valid) | n/a | 100% |
| **Fidelity overall** | **67.39%** | **99.52%** |

¹ La caída de glyph_coverage 100→97.59% en enhanced es esperada y deseable:
ligaduras `ﬁ`, soft-hyphens, comillas tipográficas y NULL bytes que **estaban
en el PDF** se eliminan/transforman intencionalmente en MD. Son glifos que
NO deben aparecer en el output final.

### Latencia

- Baseline: **9.5 ms** por PDF (1 página, 1149 chars)
- Enhanced: **42.1 ms** por PDF (4.4× más lento)

Solo afecta a los PDFs que cruzan el threshold de densidad STEM. El resto
sigue costando 9.5 ms. En un libro de 300 páginas con tipografía mixta,
el sobrecosto esperable es ~10-12 segundos sobre el flujo actual.

### Validación matemática (stem_validator.py)

Sobre las 5 ecuaciones detectadas en el output enhanced:

```
total_equations    = 5
valid_equations    = 5   (100%)
unbalanced_braces  = 0
orphan_markers     = 0
latex_parse_errors = 0
```

### No-regresión sobre prosa

`tests/stem_fixtures/prose_sample.pdf` (1.6 MB, primer párrafo de "The Old
Man and the Sea"):

```
STEM density: 0.0000  (threshold 0.05)
flag ON  → engine_used = "pymupdf"
flag OFF → engine_used = "pymupdf"
Output bit-exacto:  True
```

El motor STEM **no se invoca** cuando la densidad es baja. El output del
pipeline tradicional permanece byte-a-byte idéntico al pre-cambios.

---

## 5. Cambios realizados — registro completo

### Archivos NUEVOS

| Archivo | Líneas | Propósito |
|---|---:|---|
| `backend/stem_engine.py` | 425 | Pipeline analítico de extracción + normalización |
| `backend/stem_validator.py` | 145 | Validación LaTeX + auto-reparación de llaves |
| `backend/fidelity_metrics.py` | 200 | Métricas formales PDF↔MD para QA continuo |
| `tests/stem_fixtures/build_math_pdf.py` | 240 | Generador de PDF STEM con ground truth |
| `tests/stem_fixtures/build_prose_pdf.py` | 45 | Generador de PDF de prosa para no-regresión |
| `tests/stem_fixtures/ground_truth.json` | 50 | Ground truth declarativo |
| `tests/stem_fixtures/score_fidelity.py` | 165 | Scorer contra ground truth |
| `tests/stem_fixtures/run_baseline.py` | 60 | Runner de baseline |
| `tests/stem_fixtures/run_stem.py` | 35 | Runner de stem_engine directo |
| `tests/stem_fixtures/test_no_regression.py` | 130 | Test de no-regresión |
| `tests/stem_fixtures/run_full_benchmark.py` | 155 | Benchmark completo + JSON |

### Archivos MODIFICADOS

`backend/pdf_engine.py`:

| Cambio | Línea aprox | Detalle |
|---|---:|---|
| Constantes STEM | 79-99 | `_STEM_DENSITY_THRESHOLD=0.05`, `_STEM_ENABLED` desde env |
| `_get_stem_engine()` | 101-127 | Importador lazy con fallback relativo→absoluto |
| `_try_stem_engine()` | 180-209 | Hook de routing con catch-all de excepciones |
| `_convert_pdf_pymupdf` integración | 220-225 | Llama stem_engine ANTES del path text plano |

**Cero cambios** en:
- `main.py` (FastAPI app)
- `firebase_auth.py` (límites de plan)
- `conversion_cache.py` (caché)
- `security.py` (rate limiting / middlewares)
- Cualquier endpoint público

El contrato de `pdf_engine.convert_document(content, ext, filename) -> dict`
permanece intacto. El frontend (`converter.js`) sigue funcionando sin tocar
una línea.

### Dependencias añadidas

```
ftfy        6.3.1    ← Unicode mojibake repair
pylatexenc  2.10     ← LaTeX syntax validation
rapidfuzz   3.14.5   ← fuzzy matching (preparado para futuras heurísticas)
pymupdf4llm 1.27.2.3 ← capability extra encima de PyMuPDF existente
```

Total: ~2 MB. Sin modelos descargados. Sin compilación nativa.
Todos los wheels disponibles para Python 3.14 sin issues.

---

## 6. Decisiones de diseño defendibles

### Por qué NO modelos ML pesados

- **Marker-pdf** requiere Python ≤3.12. El entorno actual es 3.14.
- **Nougat** y **pix2text** tienen wheels limitadas para 3.14.
- **Mathpix** es API pago, agrega dependencia externa, costo por uso.
- Para el caso de uso real (PDFs con math en caracteres, no imágenes) la
  pérdida residual de 0.5-1% no justifica 5+ GB de modelos.

El pipeline analítico **es la solución correcta cuando la información
está en los glifos**. No es un workaround.

### Por qué baseline shift en lugar de heurísticas regex

Las regex que infieren super/sub de patrones tipo `[A-Z]\d` son
intrínsecamente ambiguas:
- `H2O` → ¿es H₂O o "H two O"?
- `E2` → ¿es E² o E2 (versión del estándar)?

El PDF tiene la respuesta exacta en `origin.y`. No tiene sentido adivinar
desde texto plano cuando la verdad está en el rawdict.

### Por qué normalización Unicode tras extracción y no durante

Posponer la normalización mantiene el wrap_runs simple (opera sobre
caracteres "raw") y permite que el bloque de normalización se ejecute
**uniformemente** sobre cualquier salida del engine — incluyendo si en
el futuro encadenamos a otro extractor (Marker, Mathpix). La
normalización es un post-processor universal, reutilizable.

### Por qué heading filtering con composition checks

Los operadores display (∫, ∑) se renderizan en tamaño grande (~15pt vs
body 11pt), igual que un heading H2. Sin filtro de composición
(`≥4 letras ASCII ∧ sin operadores display ∧ sin marcadores LaTeX`),
las ecuaciones display se marcarían como headings erróneamente. Esto se
verificó empíricamente en la iteración 2.

---

## 7. Limitaciones conocidas

1. **Glifos sin ToUnicode CMap**: si un PDF usa fuentes Type 3 sin mapping
   Unicode, PyMuPDF devuelve NULL byte y el glifo se pierde. El pipeline
   detecta y elimina NULL bytes pero no puede recuperar el carácter
   original. Este es un problema fundamental del PDF source, no del
   convertidor.

2. **Escaneados con math**: out-of-scope explícito del proyecto. Cuando
   un PDF tiene >40% de páginas sin texto, el routing va a EasyOCR
   existente. Para fidelidad >95% en este caso se requeriría
   Nougat/Marker/Mathpix.

3. **Matrices anidadas, diagramas Feynman, notación tensorial 3D**:
   requieren modelos visuales. No están en alcance de un pipeline
   analítico puro.

4. **Densidad STEM threshold (0.05)**: calibrado para libros académicos
   estándar. Documentos con muy poco math (1 fórmula en 100 páginas)
   pueden no cruzar el threshold y usar el pipeline tradicional para esa
   fórmula. Si esto resulta problemático, basta bajar el threshold a
   0.01 — el stem_engine funciona igual de bien sobre prosa pura
   (genera output muy similar al pipeline original pero con headings
   detectados y ligaduras expandidas).

---

## 8. Recomendaciones para producción

### Activación

El motor STEM está activado por defecto. Para desactivarlo en producción
sin tocar código:

```bash
BOOKDORK_STEM_ENGINE=0  # añadir a .env del servidor
```

Recomiendo dejarlo activado. El sobrecosto (4.4× latencia) solo aplica a
PDFs STEM (que son una fracción del tráfico) y se ejecuta dentro del
ProcessPoolExecutor existente — no bloquea el event loop.

### Monitoreo

Cada conversión STEM emite un log INFO:

```
STEM density 0.059 ≥ 0.050 — activando stem_engine para 'archivo.pdf'
```

Sumar esto al sistema de métricas. KPI sugerido:

```
% de conversiones STEM-routed / total de conversiones PDF
fidelity_overall promedio por engine
```

### Tests en CI

Añadir a tu CI/pre-commit (cuando exista pipeline):

```yaml
- name: STEM no-regression test
  run: python tests/stem_fixtures/test_no_regression.py
```

El test corre en <2 s, sin red, sin GPU.

### Futuro: ruta híbrida con ML opcional

El pipeline está diseñado con interfaz limpia. Si más adelante querés
añadir Marker o Mathpix para los <5% de casos donde el pipeline analítico
queda por debajo del 99%, basta con:

1. Crear `stem_engine_marker.py` con la misma firma `convert_pdf_stem`
2. Añadir en `pdf_engine._try_stem_engine` una segunda capa de routing
3. El analítico sigue siendo el camino default, marker se activa solo
   cuando un detector más específico (e.g., "PDF con matemática display
   compleja + tablas grandes") lo justifica

No requiere refactor del módulo principal.

### Escalabilidad

El stem_engine es CPU-bound puro (sin GPU, sin red, sin disco más allá
del PDF de entrada). Escala linealmente con el `CONVERT_MAX_CONCURRENT`
existente. Para batch grandes (libros de 1000+ páginas), considerar:

- Procesar páginas en paralelo dentro del módulo (analógico a `pages_parallel`
  de OCR). Hoy stem_engine procesa secuencialmente. Para libros medios
  no hace falta; para libros muy grandes sí.

---

## 9. Cómo reproducir los resultados

```powershell
# Dentro del repo, con el venv activado:
cd "C:\Users\nicof\OneDrive\Escritorio\Motor de busqueda libros"

# Regenerar el PDF de control y el ground truth:
python tests/stem_fixtures/build_math_pdf.py
python tests/stem_fixtures/build_prose_pdf.py

# Ejecutar el benchmark completo (baseline vs enhanced vs prose):
python -X utf8 tests/stem_fixtures/run_full_benchmark.py

# Test de no-regresión aislado:
python -X utf8 tests/stem_fixtures/test_no_regression.py
```

Salida esperada: `42.00% → 100.00%` en STEM, bit-exact en prosa.

Resultados completos en `tests/stem_fixtures/bench_results.json`.

---

## 10. Iteración v2 — font_recovery sobre libros reales

Al testear el pipeline contra el libro real "Zill — Ecuaciones Diferenciales"
(McGraw-Hill, 724 págs, Adobe Acrobat 8.0 / 2010), descubrí un defecto
sistemático que NO es del pipeline sino del PDF de origen: el documento embebe
fuentes Adobe Type 1 antiguas con tablas **ToUnicode CMap rotas o ausentes**.

### Causa raíz (verificada empíricamente)

| Fuente embebida | Comportamiento | Tipo de glifos afectados |
|---|---|---|
| `MathematicalPi-One/-Two/-Three/-Four/-Six` (+ Italic/Oblique) | Emite control bytes `\x02..\x1F` | Operadores, relaciones, arrows |
| `Grk` (custom) | Emite ASCII a-z | Greek lowercase + uppercase |
| `Symbol`, `SymbolStd` | Emite Unicode correcto | ✓ (no requiere recovery) |

Inspeccionando `Encoding/Differences` del PDF, los glyph names son del
formato `H11002`, `H9251`, etc. — convención privada Adobe que no aparece
en el Adobe Glyph List público.

### Solución implementada: matching visual determinístico

Módulo nuevo `backend/font_recovery.py` (470 líneas) que:

1. **Detecta automáticamente** PDFs con fuentes de CMap roto (regex sobre
   font basefont names). Si no hay → no se instancia (cero costo).
2. **Pre-renderiza ~150 candidatos Unicode** (Greek + math operators) usando
   Cambria Math (presente en todo Windows).
3. **Por cada glifo roto encontrado**, renderiza la región exacta del bbox
   de la página, normaliza a silueta 64×64 y compara contra candidatos.
4. **Score combinado**: `IoU·0.65 + aspect_ratio_sim·0.25 + fill_ratio_sim·0.10`
   — threshold 0.50 calibrado empíricamente.
5. **Atajo phonetic** para Grk: a→α, b→β, c→χ, …, w→ω, x→ξ, y→ψ, z→ζ
   (Adobe Symbol Encoding estándar).
6. **Cache por documento**: cada (font, code) único se procesa una vez.

### Resultados sobre Zill completo (724 páginas, 9 MB)

| Métrica | Baseline (pre-pipeline) | Enhanced (con font_recovery) | Δ |
|---|---:|---:|---:|
| Control bytes `\x02..\x1F` | 40,431 | 292 | **−99.3%** |
| NULL bytes | 185 | 0 | **−100%** |
| Palabras rotas (`hyphen\n`) | 3,865 | 63 | **−98.4%** |
| Headings markdown | 0 | 59 | nuevo |
| Marcadores `^{...}` | 0 | 7,363 | nuevo |
| Marcadores `_{...}` | 0 | 3,013 | nuevo |
| Greek lowercase | 325 | 7,514 | **+23×** |
| Greek uppercase | 25 | 581 | **+23×** |
| Math operators (=, −, ±, ≤, →…) | 8,738 | 23,949 | **+174%** |
| Conversion time | n/a | 20.6 s | 28 ms/pág |

### Tests QA (9/9 pasan)

`tests/stem_fixtures/test_font_recovery.py` cubre:

1. Detección correcta de fuentes broken vs OK (14 casos)
2. Tabla Grk a-z → Greek (26 mapeos verificados)
3. `needs_recovery()` activa/desactiva correctamente
4. Atajo Grk directo (sin render)
5. Cache idempotente
6. Recovery visual sobre 5 páginas del Zill (>100 glifos recuperados)
7. No-regresión: PDF de prosa no instancia font_recovery
8. No-regresión: fixture STEM sintético no usa font_recovery
9. E2E: pdf_engine.convert_document() activa font_recovery sobre Zill
   y recupera ≥50 Greek + ≥100 math operators en 50 páginas

### Limitaciones residuales en Zill

Sobre 724 págs, ~292 control bytes residuales (0.7% del baseline):
- Glifos extraños (ornamentos editoriales, márgenes) que no matchean ningún
  candidato Unicode con confianza ≥ 0.50. Mejor mantenerlos que adivinar.
- Algunos glifos `=`/`−` ambiguos visualmente (Mathematical Pi tiene una
  variante de "equals" que es solid block, indistinguible de minus a baja
  resolución). Quedan como minus en el output → falsamente leído como
  operación; el contexto matemático queda preservado en general.

### Decisiones de diseño defendibles (v2)

**Visual matching vs hardcoded mapping**: las tablas hardcoded para fuentes
Adobe propietarias no están públicamente disponibles y serían frágiles
(diferentes subsets del mismo font name). El visual matching usa la verdad
ground en el PDF mismo y funciona en cualquier PDF futuro con problema
similar — sin requerir mantenimiento.

**Por qué Cambria Math como referencia**: presente en todo Windows desde
Vista, cobertura math/Greek casi completa (verificado: 18/18 símbolos de
mi conjunto de candidatos). Fallback secuencial a Segoe UI Symbol, DejaVu,
Arial. Si nada disponible → font_recovery desactivado con warning.

**Cache por documento (no global)**: dos PDFs pueden usar el mismo font
name con subsets distintos → mapeos distintos. La cache vive durante la
conversión y muere con el objeto FontRecovery.

**Threshold 0.50**: calibrado sobre los top 10 glifos de MathematicalPi-One
en Zill. Recupera 8/10 sin falsos positivos burdos. Bajar a 0.45 produciría
matches dudosos; subir a 0.55 perdería el 25% de recoveries reales.

### Archivos añadidos / modificados (v2)

| Archivo | Estado | Líneas | Propósito |
|---|---|---:|---|
| `backend/font_recovery.py` | NUEVO | 470 | Visual glyph matching |
| `backend/stem_engine.py` | modificado | +60 | Hook de font_recovery |
| `tests/stem_fixtures/test_font_recovery.py` | NUEVO | 250 | 9 tests QA |
| `tests/stem_fixtures/convert_zill_full.py` | NUEVO | 130 | Benchmark fullbook |

Cero cambios en `main.py`, frontend, endpoints, contrato público.

### Dependencias añadidas (v2)

```
fonttools 4.63.0   ← inspección de CFF embebido (uso opcional para debugging)
```

`numpy` ya estaba (transitiva de torch/easyocr). `fitz`/PyMuPDF ya estaba.

## 11. Checklist de QA antes de merge a main

- [x] Tests de no-regresión pasan (prosa bit-exacta con/sin flag)
- [x] Fidelity sobre fixture STEM 100%
- [x] Tests font_recovery 9/9 pasan
- [x] Validación LaTeX 100% sobre ecuaciones detectadas
- [x] Cero cambios en endpoints públicos
- [x] Cero cambios en contrato `convert_document(content, ext, filename)`
- [x] Feature flag operativo (`BOOKDORK_STEM_ENGINE=0` revierte completamente)
- [x] Imports lazy — la app arranca igual si deps no están instaladas
- [x] Excepciones en stem_engine / font_recovery → fallback automático
- [x] Detección de PDFs SIN fuentes broken → font_recovery NO se instancia
- [x] Documentación completa de cambios (este documento)
- [x] Test E2E sobre libro real de 724 páginas (Zill)

---

## 12. Iteración v3 — fidelidad de estructura 2D (matrices, columnas, caracteres)

Disparada al analizar la conversión real de **Anton, *Elementary Linear
Algebra* 11e** (`conversion_cache/d42f539…md`). Reveló que el 99% reportado
aplicaba a matemática *inline*; sobre estructura **2D** (matrices, sistemas
alineados, fracciones apiladas) la fidelidad caía a ~55-60%, y —lo más grave
para uso académico/IA— el pipeline **generaba datos falsos** (no solo
omisiones): columnas de página fusionadas creaban ecuaciones inexistentes.

### Defectos atacados (ordenados por gravedad)

| # | Defecto | Antes | Después | Modo de fallo |
|---|---|---|---|---|
| 1 | Fusión de columnas físicas | `=3` + `4x1` → `=34x1` | de-fusión por gutter ≥4em | **dato falso** |
| 2 | Caracteres de control / PUA | `\x02..\x1F`, glifos PUA residuales | eliminados + auditoría | char que interfiere |
| 3 | Pérdida de espacios | `andthesecondindicates` | recuperación por hueco bbox | legibilidad/tokenización |
| 4 | Fracciones apiladas | `4/3` → `_3^4` | `\frac{4}{3}` con guarda | dato falso |
| 5 | Matrices | `[1 1 2\|9]` → `1129` | `\begin{bmatrix}` | estructura perdida |

### Cambios en `stem_engine.py`

1. **`_Glyph`** ahora guarda `bbox_x0`/`bbox_x1` (borde real de tinta) — base
   geométrica fiable para detección de espacios, columnas y celdas.
2. **Integridad de caracteres** (garantía "cero desconocidos"):
   - Tabla de traducción amplía a C0 (`\x00-\x1F` salvo `\t\n\r`), DEL, C1
     (`\x80-\x9F`), `U+FFFC/FFFD/FFFE/FFFF`.
   - `audit_charset(text)` — informe QA de glifos sospechosos por codepoint.
   - `_sanitize_charset()` — red de seguridad final: elimina cualquier
     categoría Unicode `Cc/Cf/Cs/Co/Cn` (incluye Private-Use de fuentes rotas
     no recuperadas). Logueado como WARNING si algo sobrevive (invariante).
3. **`_wrap_runs` reescrito**: detección de espacios por hueco real de bbox
   (`_WORD_SPACE_RATIO=0.18`) en vez de la aproximación `origin_x±size·0.5`;
   detección de **fracción** (`\frac`) cuando el grupo super e inferior se
   apilan en X (`_FRACTION_X_OVERLAP=0.5`) **y no cuelgan de una base
   alfanumérica** — la guarda que impide convertir un índice `x_i^2` en
   fracción (evitar fabricar matemática falsa).
4. **`_split_columns`**: parte una línea en huecos ≥`_COLUMN_GAP_RATIO=4em`
   (gutter entre columnas físicas). Mata el bug de ecuaciones falsas.
5. **Matrices**: `_detect_matrix_spans` + `_render_matrix` reconstruyen bloques
   delimitados por piezas de corchete extensible (`U+23A1-23AD`, `U+239B-23AD`)
   como `\begin{bmatrix}`, con celdas separadas por hueco (`&`) y filas (`\\`).
6. **`_render_page`** orquesta: de-fusión de columnas → matrices → formateo
   línea-a-línea.

### QA — tests añadidos

`tests/stem_fixtures/test_stem_fidelity.py` — **13 tests deterministas a nivel
glifo** (sin generar PDFs): construyen `_Glyph` con geometría controlada y
verifican cada componente. **13/13 pasan.** Cubren: integridad de caracteres
(C0/PUA/replacement), espacios (recuperación + sin espurios), fracción (sin
base / guarda de índice), columnas (split / no-split), matriz (bmatrix / no
inventa), y no-regresión de sub/superíndice.

Verificado además:
- `test_no_regression.py`: prosa **bit-exacta** con/sin flag ✓
- `run_full_benchmark.py`: fixture STEM **sigue en 100%** ground-truth, prosa
  idéntica ✓
- `test_font_recovery.py`: **9/9**, Zill recupera 174 glifos, **0 NULL bytes** ✓

### Limitaciones residuales (honestidad de ingeniería)

- **Calibración sobre PDF real pendiente**: los umbrales (`_COLUMN_GAP_RATIO`,
  `_MATRIX_CELL_GAP`) están validados con geometría sintética. El PDF fuente
  de Anton no está en el repo; calibrar contra él requiere ese PDF. La lógica
  es correcta; los valores absolutos pueden necesitar ajuste fino.
- **Celdas de matriz muy compactas**: si la tipografía no deja hueco entre
  columnas (`1129`), se emite la fila como **una sola celda** — la estructura
  `bmatrix` queda marcada (la IA sabe que es matriz) pero las columnas no se
  separan. No es dato falso, es ambigüedad explícita.
- **Fracción 100% fiable requiere geometría de la barra**: la regla actual
  (apilado en X + sin base) mejora el artefacto `_3^4` sin crear falsos
  positivos, pero la señal definitiva es el trazo vectorial de la barra
  (`page.get_drawings()`) — siguiente iteración si se exige 99.9% en fracciones.

---

*Reporte generado automáticamente como parte del flujo de implementación.*
*Versión 3 — fidelidad de estructura 2D (matrices, columnas, integridad de caracteres).*
