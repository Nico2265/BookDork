# Reporte técnico — Conservación de calidad en la conversión PDF→Markdown de contenido matemático

**Caso:** Zill, *Advanced Engineering Mathematics* 6ª ed. (Jones & Bartlett, 1047 pp).
**Objetivo:** salida apta para IA — **cero errores en fórmulas matemáticas**.
**Fecha:** 2026-05-29.

---

## 1. Resumen ejecutivo

La conversión perdía calidad **en la primera etapa del pipeline** (traducción glifo→Unicode); todo lo demás solo *propagaba o disfrazaba* esa pérdida. Verificado con evidencia en los dos motores del stack:

- **MarkItDown (Microsoft / pdfminer):** emite `x (cid:3) 4` donde debe decir `x = 4`. Vuelca el índice de glifo crudo porque la tabla ToUnicode está rota, y además **destruye el layout** (inventa tablas Markdown, mezcla el orden de lectura).
- **stem_engine (PyMuPDF + matcher visual):** emitía `x = 4` con muchos operadores correctos, pero confundía **`=` → `⇋`** de forma silenciosa y plausible (*error que engaña*).

**Resultado de la corrección implementada** (canal de nombres determinista integrado en `stem_engine`):

| Métrica (36 páginas, 360–395) | Antes (solo visual) | Después (canal de nombres) |
|---|---:|---:|
| Operadores que engañan (`⇋` etc.) | **1299** | **0** |
| Signos `=` correctos | 10 | 1016 |

---

## 2. Desglose del proceso de conversión

```
PDF ─► [E0] Content stream     operadores Tj/TJ: bytes + recurso de fuente + posición
    ─► [E1] Glifo → Unicode     aplica ToUnicode CMap del font  ◄── AQUÍ NACE EL ERROR
    ─► [E2] Recuperación        (stem_engine) matching visual de fuentes rotas
    ─► [E3] Estructura 2D        super/sub, fracciones, matrices, orden de lectura
    ─► [E4] Normalización        ftfy, ligaduras, _sanitize_charset (¡borra!)
    ─► [E5] Serialización MD
```

Cada flecha es un **canal con pérdida**. En E0 la información es completa (código de glifo + programa de fuente + geometría + posición). El problema empieza en E1.

## 3. Localización exacta del error (con evidencia)

**E1 — Glifo→Unicode es la raíz.** Las fuentes `MathematicalPi-*` traen ToUnicode rota/ausente. El mapa de decodificación `T : código → símbolo Unicode` está corrupto. Cada motor reacciona distinto:

| Motor | En E1 | Resultado | Tipo de error |
|---|---|---|---|
| MarkItDown/pdfminer | vuelca índice crudo | `(cid:3)`, `(cid:4)`, `(cid:8)` | visible pero ilegible + layout roto en E3 |
| PyMuPDF | emite código de control | `\x03`… | pasa a E2 |
| stem_engine (E2) | invierte por forma visual | `=` → `⇋` | **que engaña** (válido pero falso) |
| `_sanitize_charset` (E4) | borra lo no mapeado | desaparece `Π` | **silencioso** |

## 4. Diagnóstico matemático

El sistema intentaba **invertir un mapa destruido** `T` usando una observación **también no inyectiva**: el matcher visual usa `φ : bitmap → ℝ³` (IoU, aspecto, relleno), y símbolos distintos (`=`, `≡`, `⇋`) **colapsan al mismo vector** → el `argmax` tiene núcleo no trivial y elige mal.

Dos teoremas marcan el terreno:

1. **Desigualdad de procesamiento de datos.** En la cadena de Markov `S → X_E1 → X_E5`, `I(S; X_E5) ≤ I(S; X_E1)`: la información que E1 destruye no existe en la salida. **Corolario:** no se puede reparar la calidad desde el Markdown de MarkItDown — su texto `(cid:N)` está más allá del cuello de botella. **Hay que intervenir en E1/E2**, donde la geometría del glifo y el programa de fuente todavía existen.
2. **No inyectividad ⇒ inversión mal planteada**, que requiere regularización (más información o mejor descriptor).

## 5. Solución implementada (corto plazo, determinista)

El descubrimiento clave: **el PDF conserva el NOMBRE de cada glifo** en su `/Encoding /Differences` (esquema Adobe `H<número>`), información determinista que el pipeline ignoraba. Se añadió un **canal de nombres** que opera en E2 **antes** del matcher visual:

1. Lee `code → nombre` del `/Differences` (resuelto por `(xref, code)` per-página).
2. Resuelve `nombre → Unicode` vía Adobe Glyph List + un **suplemento verificado visualmente** (`H11005`=`=`, `H11001`=`+`, `H11002`=`−`, `H9024`=`Ω`, `HS11005`=`≠`, …).
3. Si el nombre es desconocido → **se abstiene** (nunca adivina); cae al matcher visual.

Módulos: `backend/glyph_name_recovery.py`, `glyph_name_supplement.json`. Tests: `tests/test_glyph_name_recovery.py` (23). Banco/harvest: `tests/bench_glyph_name_recovery.py`.

**Resultado medido (libro completo, resolución correcta por (xref,code)):** 94.2 % de glifos matemáticos rotos recuperados deterministamente, 0 % adivinanza; el resto se abstiene y es cosechable (verificar ~10 nombres más → ~99 %). A/B en 36 páginas: **1299 → 0** operadores que engañan.

## 6. Solución de fondo (matemáticas avanzadas, para el residuo y fuentes sin nombre)

El canal de nombres no cubre fuentes CID, escaneados ni glifos sin nombre. Para esos, el marco correcto es un **problema inverso bayesiano**:

- **Pilar A — observación inyectiva (geometría + topología):** reemplazar IoU por **distancia de transporte óptimo (Wasserstein-2)** entre medidas de tinta y por **homología persistente** (firma topológica estable). Separan `=`/`⇋`/`≡` que IoU colapsa.
- **Pilar B — decodificación bayesiana (redundancia del lenguaje):** modelar la línea como **CRF lineal** y resolver `argmax_s Σ log P(Φ_i|s_i) + log P(s)` por **Viterbi**, con `P(s)` un modelo de notación (n-grama / PCFG). El contexto hace `P(=) ≫ P(⇋)` en una ecuación. Es decodificar a la palabra-código válida más cercana.
- **Pilar C — garantía (regla de Chow):** emitir solo si la posterior ≥ τ (derivado de `C_engaña/(C_engaña+C_visible)`), si no **abstención visible** `⟦?⟧`. Acota la tasa de errores-que-engañan y elimina el borrado silencioso de E4.

El modelo gráfico del Pilar B **fusiona canales** de forma natural y **subsume** el canal de nombres como un potencial de observación de alta confianza.

## 7. Estado y roadmap

| Fase | Acción | Estado |
|---|---|---|
| 1 | Canal de nombres determinista en E2, antes del visual | ✅ **implementado y medido (1299→0)** |
| 2 | Verificar nombres por render visual → poblar suplemento | ✅ **28 operadores verificados; recuperación 94.2 %→98.2 %; marcadores 150→22 en muestra; 0 % adivinanza** |
| 2b | Tests de regresión que blindan los bugs parchados | ✅ **6 tests (=≠⇋, operadores, no-borrado, integridad suplemento); 33 tests totales en verde** |
| 3 | Abstención visible `⟦?nombre⟧` reemplaza el borrado silencioso de E4 | ✅ **implementado y medido** (pág 371: el `Π`=`H5116` que desaparecía ahora es `⟦?H5116⟧`; 150 símbolos antes borrados ahora visibles en 36 págs; audit limpio; 0 marcadores en docs sanos) |
| 4 | OT + homología persistente en el matcher visual (residuo / CID / OCR) | pendiente (matemáticas avanzadas) |
| 5 | CRF + Viterbi con prior de notación; gold set y matriz de confusión estratificada | pendiente |
| — | Gap 2D (fracciones partidas, matrices) — problema de layout, frente separado | conocido, no abordado aquí |

## 8. Veredicto

El error era **estructural y estaba en E1**. La corrección implementada elimina la clase de error más peligrosa (operadores que engañan: `=`→`⇋`) de forma **determinista y medible**, con abstención explícita donde no hay certeza. Para el residuo y las fuentes sin nombre, la vía sólida es tratar la conversión como un **problema inverso bayesiano regularizado por geometría/topología y por la gramática de la notación**, operando siempre en la etapa donde la información todavía existe — nunca sobre la salida ya colapsada de MarkItDown.
