"""
=============================================================================
font_recovery.py — Recuperación visual de glifos en fuentes con CMap rota
=============================================================================
Muchos libros académicos (sobre todo traducciones español de Pearson/McGraw-
Hill 2005-2012) embeben fuentes Adobe Type 1 con tablas ToUnicode CMap rotas
o ausentes. Cuando PyMuPDF (o cualquier otro extractor) procesa estos PDFs,
emite el código de glifo crudo (ej: `\x02`, `\x05`) en lugar del Unicode real.

Fuentes problemáticas conocidas (detectadas empíricamente en Zill 2008):
  - MathematicalPi-One, -Two, -Three, -Four, -Six (+ variantes Bold/Italic)
  - Grk (custom Greek font) — emite ASCII a-z en lugar de Greek
  - EuclidSymbol — variante propietaria de Symbol

Symbol y SymbolStd YA funcionan bien con PyMuPDF (no requieren recovery).

ESTRATEGIA: matching visual determinístico
─────────────────────────────────────────
1. Pre-renderizar set de candidatos Unicode math/Greek (~200 chars) usando
   una fuente robusta (Cambria Math, presente en todo Windows).
2. Por cada (font, code) detectado como roto, renderizar el GLIFO REAL desde
   la región bbox de la página (rawdict provee coordenadas exactas).
3. Comparar el glifo real contra cada referencia usando IoU sobre máscaras
   binarizadas + normalizadas a 64x64.
4. Si el mejor match supera threshold 0.55, sustituir el char. Cachear
   la decisión por documento.

Esto NO usa OCR ni ML. Es matching geométrico puro: compara siluetas.
=============================================================================
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable, Optional

import fitz

logger = logging.getLogger("bookdork.font_recovery")

# ─────────────────────────────────────────────────────────────────────────────
# Fuentes problemáticas + heurísticas de detección
# ─────────────────────────────────────────────────────────────────────────────

# Regex de basefont names que típicamente tienen CMap roto. Match case-sensitive.
_BROKEN_FONT_PATTERNS = (
    re.compile(r"MathematicalPi", re.IGNORECASE),
    re.compile(r"MathPi", re.IGNORECASE),
    re.compile(r"\bGrk\b"),
    re.compile(r"\bSym\b"),  # NO "Symbol", solo "Sym" como nombre standalone
    re.compile(r"EuclidSymbol", re.IGNORECASE),
)

# Subconjunto de patrones cuando el char emitido es ASCII a-z (Grk-style).
# Adobe Symbol Encoding mapea letras Latin a Greek por proximidad fonética:
#   a→α  b→β  c→χ  d→δ  e→ε  f→φ  g→γ  h→η  i→ι  j→ϕ  k→κ  l→λ  m→μ
#   n→ν  o→ο  p→π  q→θ  r→ρ  s→σ  t→τ  u→υ  v→ϖ  w→ω  x→ξ  y→ψ  z→ζ
# Mayúsculas: A→Α  B→Β  C→Χ  D→Δ  E→Ε  F→Φ  G→Γ  H→Η  I→Ι  K→Κ  L→Λ
#             M→Μ  N→Ν  O→Ο  P→Π  Q→Θ  R→Ρ  S→Σ  T→Τ  U→Υ  W→Ω  X→Ξ  Y→Ψ  Z→Ζ
_GRK_ALPHA_MAP = {
    "a": "α", "b": "β", "c": "χ", "d": "δ", "e": "ε", "f": "φ", "g": "γ",
    "h": "η", "i": "ι", "j": "ϕ", "k": "κ", "l": "λ", "m": "μ", "n": "ν",
    "o": "ο", "p": "π", "q": "θ", "r": "ρ", "s": "σ", "t": "τ", "u": "υ",
    "v": "ϖ", "w": "ω", "x": "ξ", "y": "ψ", "z": "ζ",
    "A": "Α", "B": "Β", "C": "Χ", "D": "Δ", "E": "Ε", "F": "Φ", "G": "Γ",
    "H": "Η", "I": "Ι", "J": "ϑ", "K": "Κ", "L": "Λ", "M": "Μ", "N": "Ν",
    "O": "Ο", "P": "Π", "Q": "Θ", "R": "Ρ", "S": "Σ", "T": "Τ", "U": "Υ",
    "W": "Ω", "X": "Ξ", "Y": "Ψ", "Z": "Ζ",
}

# Candidatos por defecto para matching visual. Ordenados por frecuencia
# esperada en textos STEM. ~150 chars.
DEFAULT_CANDIDATES = (
    # Relaciones y operadores arítmeticos (más frecuentes en math)
    "= − + × ÷ ± · ∓ ≠ ≤ ≥ ≈ ≡ ≅ ∼ ∝ < > ≪ ≫ ∗ ⋅ ⊕ ⊖ ⊗ ⊘ ⊙"
    # Lógica y conjuntos
    " ∈ ∉ ∋ ∌ ⊂ ⊃ ⊆ ⊇ ⊄ ⊅ ∪ ∩ ∀ ∃ ∄ ¬ ∧ ∨ ∅"
    # Operadores grandes (display)
    " ∫ ∮ ∯ ∰ ∑ ∏ ∐ √ ∛ ∜ ∂ ∇ ∆"
    # Símbolos especiales
    " ∞ ℵ ℏ ℎ ℓ ℘ ℜ ℑ ℕ ℤ ℚ ℝ ℂ"
    # Flechas
    " → ← ↑ ↓ ↔ ⇒ ⇐ ⇔ ↦ ⇌ ⇋ ⟹ ⟸ ⟺"
    # Greek lowercase
    " α β γ δ ε ϵ ζ η θ ϑ ι κ λ μ ν ξ ο π ϖ ρ ϱ σ ς τ υ φ ϕ χ ψ ω"
    # Greek uppercase (los visualmente distintos a Latin)
    " Γ Δ Θ Λ Ξ Π Σ Υ Φ Ψ Ω"
    # Misc matemáticos
    " ′ ″ ‴ ° ∠ ∡ ⊥ ∥ ∴ ∵ ⋮ ⋯ ⋰ ⋱"
    # Paréntesis grandes (cuando aparecen como glyphs separados)
    " ( ) [ ] { } ⟨ ⟩ | ‖"
).split()

# Fuentes de referencia a probar en orden (cobertura math/Greek decreciente)
_REFERENCE_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\cambria.ttc",       # Cambria Math: máxima cobertura
    r"C:\Windows\Fonts\seguisym.ttf",      # Segoe UI Symbol: dingbats + arrows
    r"C:\Windows\Fonts\segoeuisl.ttf",     # Segoe UI fallback
    r"C:\Windows\Fonts\arial.ttf",         # último recurso
)


def _font_is_broken(font_name: str) -> bool:
    """¿El font_name está en nuestra lista de fuentes con CMap roto?"""
    if not font_name:
        return False
    # PyMuPDF a veces antepone un subset prefix tipo "ABCDEF+FontName"
    name = font_name.split("+", 1)[-1]
    return any(pat.search(name) for pat in _BROKEN_FONT_PATTERNS)


def _is_grk_font(font_name: str) -> bool:
    name = font_name.split("+", 1)[-1]
    return bool(re.search(r"\bGrk\b", name))


# ─────────────────────────────────────────────────────────────────────────────
# Renderizado y comparación
# ─────────────────────────────────────────────────────────────────────────────

# Tamaño canónico para comparar siluetas (después de normalizar)
_CANONICAL_SIZE = 64

# Threshold combinado mínimo (IoU + aspect + fill) para aceptar coincidencia.
# Calibrado empíricamente sobre Zill: 0.50 recupera 8/10 de los glifos top
# en MathematicalPi-One sin falsos positivos en glifos custom.
_DEFAULT_THRESHOLD = 0.50

# Margen alrededor del bbox al renderizar el glifo (en puntos PDF)
_BBOX_MARGIN_PT = 1.0

# Resolución de render del glifo (multiplier × bbox size)
_RENDER_ZOOM = 12


def _trim_to_content(arr) -> "Optional[np.ndarray]":
    """Recorta una imagen binarizada a su bounding box no-trivial."""
    import numpy as np
    mask = arr < 128  # negro = True
    if not mask.any():
        return None
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    return arr[rows.min():rows.max() + 1, cols.min():cols.max() + 1]


def _content_aspect_ratio(arr) -> float:
    """
    Devuelve ancho/alto del contenido (bbox after trim).
    >1 = más ancho que alto (ej: minus, arrow).
    <1 = más alto que ancho (ej: vertical bar, integral).
    """
    trimmed = _trim_to_content(arr)
    if trimmed is None or trimmed.shape[0] == 0:
        return 1.0
    h, w = trimmed.shape
    return w / max(1, h)


def _content_fill_ratio(arr) -> float:
    """Porcentaje de pixels negros dentro del bbox del contenido."""
    import numpy as np
    trimmed = _trim_to_content(arr)
    if trimmed is None:
        return 0.0
    return float((trimmed < 128).sum()) / max(1, trimmed.size)


def _resize_nearest(arr, target_h: int, target_w: int):
    """Resize por nearest-neighbor sin scipy (solo numpy)."""
    import numpy as np
    src_h, src_w = arr.shape
    if src_h == 0 or src_w == 0:
        return np.full((target_h, target_w), 255, dtype=np.uint8)
    ys = (np.arange(target_h) * src_h // target_h).clip(0, src_h - 1)
    xs = (np.arange(target_w) * src_w // target_w).clip(0, src_w - 1)
    return arr[np.ix_(ys, xs)]


def _normalize_silhouette(arr, size: int = _CANONICAL_SIZE):
    """
    Recorta a content bbox, mantiene aspect ratio, resize a `size`×`size`
    centrado sobre fondo blanco. Devuelve uint8 ó None si imagen vacía.
    """
    import numpy as np
    trimmed = _trim_to_content(arr)
    if trimmed is None:
        return None
    h, w = trimmed.shape
    # Escalar preservando aspect ratio para que el lado más largo == size*0.85
    scale = (size * 0.85) / max(h, w)
    new_h = max(1, int(round(h * scale)))
    new_w = max(1, int(round(w * scale)))
    resized = _resize_nearest(trimmed, new_h, new_w)
    # Centrar en canvas size×size
    canvas = np.full((size, size), 255, dtype=np.uint8)
    y0 = (size - new_h) // 2
    x0 = (size - new_w) // 2
    canvas[y0:y0 + new_h, x0:x0 + new_w] = resized
    return canvas


def _iou_binary(a, b, thresh: int = 128) -> float:
    """IoU entre dos imágenes a/b ya normalizadas, considerando pixels < thresh como FG."""
    if a is None or b is None:
        return 0.0
    fa = a < thresh
    fb = b < thresh
    union = (fa | fb).sum()
    if union == 0:
        return 0.0
    inter = (fa & fb).sum()
    return float(inter / union)


def _render_text_in_isolated_pdf(char: str, font_path: str, size_pt: int = 80):
    """
    Renderiza un char con la fuente dada en una página minúscula y devuelve
    np.ndarray uint8 escala de grises. Si el char no se puede renderizar
    devuelve None.
    """
    import numpy as np
    try:
        doc = fitz.open()
        page = doc.new_page(width=size_pt * 2, height=size_pt * 2)
        page.insert_font(fontname="ref", fontfile=font_path)
        # Insertar a una posición donde quede contenido en la página
        page.insert_text((size_pt * 0.3, size_pt * 1.5), char,
                         fontsize=size_pt, fontname="ref")
        pix = page.get_pixmap(dpi=200, colorspace=fitz.csGRAY)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        doc.close()
        # Verificar que algo se renderizó
        if (arr < 128).sum() < 10:
            return None
        return arr
    except Exception as exc:
        logger.debug("Render fallback for %r in %s: %s", char, font_path, exc)
        return None


def _font_has_char(font_path: str, char: str) -> bool:
    """¿La fuente cubre este codepoint?"""
    try:
        f = fitz.Font(fontfile=font_path)
        return ord(char) in f.valid_codepoints()
    except Exception:
        return False


def _pick_best_font_for(char: str) -> Optional[str]:
    """De las fuentes de referencia disponibles, devuelve la primera que cubra el char."""
    for fp in _REFERENCE_FONT_CANDIDATES:
        if not Path(fp).exists():
            continue
        if _font_has_char(fp, char):
            return fp
    return None


# ─────────────────────────────────────────────────────────────────────────────
# FontRecovery — clase principal
# ─────────────────────────────────────────────────────────────────────────────

class FontRecovery:
    """
    Recuperador de glifos por matching visual. Stateful: cachea decisiones
    por (font_name, codepoint) durante la vida del objeto. Una instancia
    por documento garantiza que dos PDFs con la "misma" font pero subsets
    distintos no compartan mappings incorrectos.
    """

    def __init__(self,
                 threshold: float = _DEFAULT_THRESHOLD,
                 candidates: Iterable[str] = DEFAULT_CANDIDATES,
                 enable_grk_alpha_map: bool = True) -> None:
        self.threshold = float(threshold)
        self.candidates = tuple(candidates)
        self.enable_grk_alpha_map = bool(enable_grk_alpha_map)
        self._cache: dict[tuple[str, int], tuple[Optional[str], float]] = {}
        # ref_features: char → (silhouette, aspect_ratio, fill_ratio)
        self._ref_silhouettes: dict[str, "np.ndarray"] = {}
        self._ref_features: dict[str, tuple[float, float]] = {}
        self._refs_loaded = False
        self._disabled = False
        self._stats = {
            "calls":            0,
            "cache_hits":       0,
            "matches_high":     0,   # confidence ≥ 0.70
            "matches_low":      0,   # 0.55 ≤ confidence < 0.70 (auditable)
            "rejected":         0,   # confidence < threshold
            "grk_alpha_direct": 0,
            "render_failed":    0,
        }

    # ── carga lazy de referencias ─────────────────────────────────────────────
    def _ensure_refs_loaded(self) -> bool:
        """Pre-renderiza las siluetas de referencia. Lazy, una sola vez."""
        if self._refs_loaded or self._disabled:
            return not self._disabled
        try:
            import numpy as np  # noqa: F401
        except ImportError:
            logger.warning("font_recovery: numpy no disponible — recovery desactivada")
            self._disabled = True
            return False

        loaded = 0
        for ch in self.candidates:
            fp = _pick_best_font_for(ch)
            if fp is None:
                logger.debug("font_recovery: ningún font de referencia cubre %r", ch)
                continue
            arr = _render_text_in_isolated_pdf(ch, fp)
            if arr is None:
                continue
            sil = _normalize_silhouette(arr)
            if sil is not None:
                self._ref_silhouettes[ch] = sil
                self._ref_features[ch] = (
                    _content_aspect_ratio(arr),
                    _content_fill_ratio(arr),
                )
                loaded += 1

        if loaded == 0:
            logger.warning(
                "font_recovery: 0 candidatos renderizados (ningún font de "
                "referencia disponible). Recovery DESACTIVADA."
            )
            self._disabled = True
            return False

        logger.info("font_recovery: %d/%d siluetas de referencia cargadas.",
                    loaded, len(self.candidates))
        self._refs_loaded = True
        return True

    # ── API pública ──────────────────────────────────────────────────────────
    def needs_recovery(self, font_name: str, char: str) -> bool:
        """¿Este (font, char) requiere intento de recuperación?"""
        if not char or self._disabled:
            return False
        if not _font_is_broken(font_name):
            return False
        # Caso A: char es control byte (< 0x20) — clásico glyph-code crudo
        if ord(char) < 0x20 and not char.isspace():
            return True
        # Caso B: font Grk + ASCII letter — mapeo phonetic
        if _is_grk_font(font_name) and char.isascii() and char.isalpha():
            return True
        return False

    def recover(self, page, span: dict, char: str) -> str:
        """
        Devuelve el char recuperado o el original si no se pudo recuperar
        con confianza suficiente.

        Args:
          page: fitz.Page que contiene el char (necesario para render).
          span: dict del rawdict de PyMuPDF que contiene el char.
          char: el char crudo a recuperar.
        """
        self._stats["calls"] += 1
        font_name = span.get("font", "")
        key = (font_name, ord(char))

        # Cache hit
        if key in self._cache:
            self._stats["cache_hits"] += 1
            recovered, _ = self._cache[key]
            return recovered if recovered is not None else char

        # Atajo: Grk + ASCII alpha → mapping directo phonetic
        if (self.enable_grk_alpha_map
                and _is_grk_font(font_name)
                and char in _GRK_ALPHA_MAP):
            mapped = _GRK_ALPHA_MAP[char]
            self._cache[key] = (mapped, 1.0)
            self._stats["grk_alpha_direct"] += 1
            return mapped

        # Visual matching
        if not self._ensure_refs_loaded():
            self._cache[key] = (None, 0.0)
            return char

        # Encontrar el bbox del char en la página (buscar en span.chars)
        bbox = self._find_char_bbox(span, char)
        if bbox is None:
            self._stats["render_failed"] += 1
            self._cache[key] = (None, 0.0)
            return char

        # Renderizar el glifo real de la página
        sil = self._render_page_glyph(page, bbox)
        if sil is None:
            self._stats["render_failed"] += 1
            self._cache[key] = (None, 0.0)
            return char

        # Características del glifo del PDF (pre-normalización)
        # Necesitamos el array crudo, así que re-renderizamos sin normalizar
        raw_glyph = self._render_page_raw(page, bbox)
        if raw_glyph is None:
            self._stats["render_failed"] += 1
            self._cache[key] = (None, 0.0)
            return char
        pdf_ar = _content_aspect_ratio(raw_glyph)
        pdf_fill = _content_fill_ratio(raw_glyph)

        # Match contra todas las referencias, combinando IoU + aspect + fill
        best_char: Optional[str] = None
        best_score = 0.0
        for ref_char, ref_sil in self._ref_silhouettes.items():
            iou = _iou_binary(sil, ref_sil)
            ref_ar, ref_fill = self._ref_features[ref_char]
            # Aspect ratio similarity: 1.0 si idéntico, decae rápido
            # Usar log-ratio para tratar 5:1 y 1:5 de forma simétrica
            import math
            log_pdf = math.log(max(0.1, pdf_ar))
            log_ref = math.log(max(0.1, ref_ar))
            ar_sim = math.exp(-abs(log_pdf - log_ref))  # ∈ (0, 1]
            # Fill ratio similarity
            fill_sim = 1.0 - min(1.0, abs(pdf_fill - ref_fill) * 2.0)
            # Score combinado: IoU domina, aspect/fill como tie-breakers + boosts
            score = iou * 0.65 + ar_sim * 0.25 + fill_sim * 0.10
            if score > best_score:
                best_score = score
                best_char = ref_char

        if best_char is not None and best_score >= self.threshold:
            self._cache[key] = (best_char, best_score)
            if best_score >= 0.70:
                self._stats["matches_high"] += 1
                logger.debug("font_recovery match: %s/0x%02X → %r (IoU=%.2f)",
                             font_name, ord(char), best_char, best_score)
            else:
                self._stats["matches_low"] += 1
                logger.info("font_recovery LOW-confidence: %s/0x%02X → %r (IoU=%.2f)",
                            font_name, ord(char), best_char, best_score)
            return best_char

        # Rejected
        self._cache[key] = (None, best_score)
        self._stats["rejected"] += 1
        logger.debug("font_recovery rejected: %s/0x%02X best=%r IoU=%.2f < %.2f",
                     font_name, ord(char), best_char, best_score, self.threshold)
        return char

    def stats(self) -> dict:
        return dict(self._stats)

    # ── internals ─────────────────────────────────────────────────────────────
    def _find_char_bbox(self, span: dict, char: str) -> Optional[tuple]:
        """
        Busca el bbox del char en el array `chars` del span (rawdict mode).
        Si el span fue tomado en dict-mode no tendrá `chars`; intentamos
        usar el bbox del span entero como fallback (precision menor).
        """
        chars = span.get("chars")
        if chars:
            for ch in chars:
                if ch.get("c") == char:
                    return tuple(ch["bbox"])
            return None
        # Fallback: usar bbox del span
        if span.get("text", "") == char:
            return tuple(span["bbox"])
        return None

    def _render_page_raw(self, page, bbox: tuple) -> Optional["np.ndarray"]:
        """Renderiza la región del bbox sin normalizar (para features)."""
        import numpy as np
        try:
            expanded = fitz.Rect(
                bbox[0] - _BBOX_MARGIN_PT,
                bbox[1] - _BBOX_MARGIN_PT,
                bbox[2] + _BBOX_MARGIN_PT,
                bbox[3] + _BBOX_MARGIN_PT,
            )
            pix = page.get_pixmap(
                matrix=fitz.Matrix(_RENDER_ZOOM, _RENDER_ZOOM),
                clip=expanded,
                colorspace=fitz.csGRAY,
            )
            return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        except Exception as exc:
            logger.debug("font_recovery render error bbox=%s: %s", bbox, exc)
            return None

    def _render_page_glyph(self, page, bbox: tuple) -> Optional["np.ndarray"]:
        """Renderiza el glifo desde la página y normaliza a silueta."""
        arr = self._render_page_raw(page, bbox)
        if arr is None:
            return None
        return _normalize_silhouette(arr)
