"""Análisis forense de fidelidad del MD de Serway: busca daño real, no heurística."""
from __future__ import annotations
import re, unicodedata
from collections import Counter
from pathlib import Path

MD = Path(__file__).parent / "serway_out" / "Serway-7Ed.md"
text = MD.read_text(encoding="utf-8")
N = len(text)
print(f"== Serway MD: {N:,} chars, {len(text.splitlines()):,} líneas ==\n")

# 1. Mojibake / caracteres que ENGAÑAN (prioridad #1)
moji = {"�": "U+FFFD replacement", "￾": "U+FFFE", "￼": "U+FFFC object",
        "\x00": "NUL"}
print("--- 1. Caracteres rotos (mojibake) ---")
for ch, name in moji.items():
    c = text.count(ch)
    if c:
        print(f"  {name}: {c}")
ligs = "ﬀﬁﬂﬃﬄﬅﬆ"
lig_hits = {l: text.count(l) for l in ligs if text.count(l)}
print(f"  ligaduras sin expandir: {sum(lig_hits.values())} {lig_hits}")
# soft hyphen, non-breaking artifacts
print(f"  soft-hyphen U+00AD: {text.count(chr(0xAD))}")
print(f"  U+0080-009F (C1 control): {sum(1 for c in text if 0x80<=ord(c)<=0x9F)}")

# 2. Categorías Unicode sospechosas (Co=PUA, Cn=unassigned, Cf=format)
cats = Counter(unicodedata.category(c) for c in text)
print("\n--- 2. Categorías de control/PUA ---")
for cat in ("Cc","Cf","Co","Cs","Cn"):
    if cats.get(cat):
        ex = Counter(c for c in text if unicodedata.category(c)==cat).most_common(5)
        ex_s = ", ".join(f"U+{ord(c):04X}×{n}" for c,n in ex)
        print(f"  {cat}: {cats[cat]}  ({ex_s})")

# 3. Símbolos matemáticos / griegos presentes
print("\n--- 3. Símbolos STEM presentes ---")
greek = "αβγδεζηθικλμνξοπρστυφχψωΑΒΓΔΕΘΛΞΠΣΦΨΩ"
mathops = "∫∮∑∏∂∇√≠≤≥≈±×÷⋅∈∉∞→←⇒"
for label, charset in (("griego", greek), ("operadores", mathops)):
    present = {c: text.count(c) for c in charset if text.count(c)}
    print(f"  {label}: {len(present)}/{len(charset)} tipos · top {Counter(present).most_common(8)}")

# 4. Subíndices/superíndices Unicode SIN convertir (señal de que stem no corrió)
print("\n--- 4. Sub/superíndices Unicode crudos (no convertidos a ^/_) ---")
uni_sup = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿ"
uni_sub = "₀₁₂₃₄₅₆₇₈₉₊₋ₓ"
print(f"  superíndice Unicode (²³…): {sum(text.count(c) for c in uni_sup)}")
print(f"  subíndice Unicode (₀₁…): {sum(text.count(c) for c in uni_sub)}")
print(f"  marcadores LaTeX ^: {text.count('^')}  _: {text.count('_')}  \\frac: {text.count(chr(92)+'frac')}")

# 5. Palabras pegadas (espacios perdidos) — heurística: minúscula+Mayúscula camel
camel = re.findall(r"[a-záéíóúñ]{3,}[A-ZÁÉÍÓÚÑ][a-z]", text)
print(f"\n--- 5. Posibles espacios perdidos (camelCase) ---\n  ocurrencias: {len(camel)}  ej: {Counter(camel).most_common(6)}")

# 6. Guiones de corte de línea no reunidos ("conti- nuidad")
hyph = re.findall(r"[a-záéíóúñ]+-\s+[a-záéíóúñ]+", text)
print(f"\n--- 6. Guiones de corte sin reunir ---\n  ocurrencias: {len(hyph)}  ej: {hyph[:6]}")

# 7. Muestra de contenido con ecuaciones
print("\n--- 7. Muestras de líneas con densidad matemática ---")
lines = text.splitlines()
math_lines = [l for l in lines if sum(c in greek+mathops+uni_sup+uni_sub+"=/+−·" for c in l) >= 4 and len(l.strip())>8]
import random
random.seed(7)
for l in random.sample(math_lines, min(10, len(math_lines))):
    print(f"    | {l.strip()[:110]}")
print(f"  (total líneas mat. detectadas: {len(math_lines)})")
