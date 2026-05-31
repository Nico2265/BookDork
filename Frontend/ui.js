/**
 * =============================================================================
 * ui.js — Utilidades de interfaz de usuario para BookDork
 * =============================================================================
 * Responsable de:
 *   • Sistema de partículas de fondo (canvas)
 *   • Sistema de notificaciones toast
 *   • Comprobación de salud del servidor (health check)
 *   • Animaciones de entrada de la página
 *   • Helpers de accesibilidad
 * =============================================================================
 */

// ─────────────────────────────────────────────────────────────────────────────
// Sistema de Toast Notifications
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Muestra una notificación toast temporal en la esquina inferior derecha.
 * @param {string} message - Texto del mensaje
 * @param {'success'|'error'|'info'} type - Tipo de notificación
 * @param {number} duration - Duración en ms antes de desaparecer
 */
function showToast(message, type = 'info', duration = 3000) {
  // Asegurar que existe el contenedor
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.setAttribute('aria-live', 'polite');
    container.setAttribute('aria-atomic', 'false');
    document.body.appendChild(container);
  }

  const toast = document.createElement('div');
  toast.className = `toast toast--${type}`;
  toast.setAttribute('role', 'alert');
  toast.textContent = message;

  container.appendChild(toast);

  // Ocultar después de `duration` ms con animación de salida
  setTimeout(() => {
    toast.classList.add('is-hiding');
    toast.addEventListener('animationend', () => toast.remove(), { once: true });
  }, duration);
}

// ─────────────────────────────────────────────────────────────────────────────
// Sistema de Partículas de Fondo
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Inicializa el sistema de partículas flotantes en el canvas de fondo.
 * Las partículas simulan polvo de libros o chispas doradas.
 * Se desactiva si el usuario prefiere reducción de movimiento.
 */
function initParticles() {
  // Respetar preferencia de reducción de movimiento
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  const canvas = document.getElementById('particles-canvas');
  if (!canvas) return;

  const ctx = canvas.getContext('2d');

  // ── Partícula ──────────────────────────────────────────────────────────────
  class Particle {
    constructor() {
      this.reset(true);
    }

    reset(initial = false) {
      this.x = Math.random() * canvas.width;
      // Si es inicial, distribuir por toda la pantalla; si reaparece, desde abajo
      this.y = initial
        ? Math.random() * canvas.height
        : canvas.height + 10;
      this.size = Math.random() * 1.5 + 0.3;
      this.speedX = (Math.random() - 0.5) * 0.3;
      this.speedY = -(Math.random() * 0.4 + 0.1);
      this.opacity = Math.random() * 0.5 + 0.1;
      this.fadeSpeed = Math.random() * 0.002 + 0.001;
      // Color: dorado o azul acero, alternando aleatoriamente
      this.isGold = Math.random() > 0.4;
    }

    update() {
      this.x += this.speedX;
      this.y += this.speedY;
      this.opacity -= this.fadeSpeed;

      // Leve drift horizontal sinusoidal
      this.x += Math.sin(this.y * 0.01) * 0.2;

      // Resetear cuando sale de vista o se desvanece
      if (this.y < -10 || this.opacity <= 0) this.reset();
    }

    draw() {
      if (!ctx) return;
      ctx.save();
      ctx.globalAlpha = Math.max(0, this.opacity);
      ctx.beginPath();
      ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
      ctx.fillStyle = this.isGold ? '#d4a853' : '#5b8dd9';
      ctx.fill();
      ctx.restore();
    }
  }

  // ── Resize handler ─────────────────────────────────────────────────────────
  function resizeCanvas() {
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
  }

  resizeCanvas();
  window.addEventListener('resize', resizeCanvas, { passive: true });

  // ── Crear partículas ───────────────────────────────────────────────────────
  const PARTICLE_COUNT = Math.min(60, Math.floor(window.innerWidth / 20));
  const particles = Array.from({ length: PARTICLE_COUNT }, () => new Particle());

  // ── Loop de animación ──────────────────────────────────────────────────────
  let animationId;
  let isVisible = true;

  function animate() {
    if (!isVisible) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    particles.forEach(p => {
      p.update();
      p.draw();
    });
    animationId = requestAnimationFrame(animate);
  }

  animate();

  // Pausar cuando la pestaña está oculta (ahorra CPU)
  document.addEventListener('visibilitychange', () => {
    isVisible = !document.hidden;
    if (isVisible) animate();
    else cancelAnimationFrame(animationId);
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Health Check — Estado del servidor
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Consulta el endpoint /api/health y actualiza los indicadores del header.
 */
// Logs verbosos solo en desarrollo local. En producción solo se registra el
// código de estado, nunca el cuerpo de la respuesta (puede contener datos
// internos en mensajes de error 5xx).
const _isDev = location.hostname === 'localhost' || location.hostname === '127.0.0.1';
const _devLog = (...args) => { if (_isDev) console.error(...args); };

async function checkServerHealth() {
  const statLabel = document.querySelector('#stat-indexed .stat-label');
  const statDot = document.querySelector('#stat-indexed .stat-dot');

  if (!statLabel) return;

  const t0 = performance.now();

  try {
    const response = await fetch(`${window.location.origin}/api/health`, {
      credentials: 'omit',
      headers: { 'Accept': 'application/json', 'ngrok-skip-browser-warning': 'true' },
    });

    const elapsed = performance.now() - t0;

    if (!response.ok) {
      _devLog('[BookDork] Health check HTTP error:', response.status, response.statusText);
      if (_isDev) {
        const text = await response.text().catch(() => '');
        _devLog('[BookDork] Response body:', text.slice(0, 500));
      }
      throw new Error(`HTTP ${response.status}`);
    }

    const text = await response.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      _devLog('[BookDork] Health check: respuesta no es JSON:', text.slice(0, 500));
      throw new Error('non-json');
    }
    const isOk = data.status === 'ok';

    if (!isOk) {
      statDot.style.background = '#e05252';
      statDot.style.boxShadow  = '0 0 6px #e05252';
      statDot.title = 'Servidor caído';
    } else if (elapsed > 400) {
      statDot.style.background = '#e09a2a';
      statDot.style.boxShadow  = '0 0 6px #e09a2a';
      statDot.title = 'Alto tráfico';
    } else {
      statDot.style.background = 'var(--green)';
      statDot.style.boxShadow  = '0 0 6px var(--green)';
      statDot.title = 'Activo';
    }

  } catch (err) {
    _devLog('[BookDork] Health check falló:', err.message || err);
    if (statDot) {
      statDot.style.background = '#e05252';
      statDot.style.boxShadow  = '0 0 6px #e05252';
      statDot.title = 'Servidor caído';
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Efectos visuales del formulario
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Añade efectos visuales al panel de filtros:
 * - Indica visualmente si un filtro tiene valor seleccionado
 */
function initFilterEffects() {
  const selects = document.querySelectorAll('.filter-select');
  const inputs  = document.querySelectorAll('.filter-input');

  function markActive(el) {
    const isActive = el.value && el.value !== 'any' && el.value !== '';
    el.style.borderColor = isActive
      ? 'var(--border-accent)'
      : '';
    el.style.color = isActive
      ? 'var(--gold-main)'
      : '';
  }

  selects.forEach(s => {
    markActive(s);
    s.addEventListener('change', () => markActive(s));
  });

  inputs.forEach(i => {
    markActive(i);
    i.addEventListener('input', () => markActive(i));
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Atajo de teclado — Mostrar ayuda de atajos
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Muestra un breve toast con los atajos de teclado disponibles
 * cuando el usuario pulsa '?'.
 */
function initKeyboardHelp() {
  document.addEventListener('keydown', (e) => {
    if (
      e.key === '?' &&
      document.activeElement.tagName !== 'INPUT' &&
      document.activeElement.tagName !== 'TEXTAREA'
    ) {
      showToast('/ → buscar  ·  Esc → limpiar  ·  Enter → buscar', 'info', 4000);
    }
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Cursor personalizado (partícula dorada que sigue al ratón)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Crea un efecto de cursor con partícula dorada.
 * Solo en escritorio y si no se prefiere reducción de movimiento.
 */
function initCustomCursor() {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  if ('ontouchstart' in window) return; // Dispositivos táctiles: sin cursor

  const cursor = document.createElement('div');
  cursor.style.cssText = `
    position: fixed;
    width: 6px;
    height: 6px;
    background: rgba(212, 168, 83, 0.6);
    border-radius: 50%;
    pointer-events: none;
    z-index: 9999;
    transition: transform 0.15s, opacity 0.3s;
    mix-blend-mode: screen;
    transform: translate(-50%, -50%);
  `;
  document.body.appendChild(cursor);

  let mouseX = -100, mouseY = -100;
  let rafId;

  document.addEventListener('mousemove', (e) => {
    mouseX = e.clientX;
    mouseY = e.clientY;
  }, { passive: true });

  function updateCursor() {
    cursor.style.left = `${mouseX}px`;
    cursor.style.top  = `${mouseY}px`;
    rafId = requestAnimationFrame(updateCursor);
  }

  updateCursor();

  // Agrandar sobre elementos interactivos
  document.addEventListener('mouseover', (e) => {
    const isInteractive = e.target.matches('a, button, input, select, [role="button"]');
    cursor.style.transform = `translate(-50%, -50%) scale(${isInteractive ? 2.5 : 1})`;
    cursor.style.opacity = isInteractive ? '0.8' : '0.6';
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Accesibilidad — Anunciar cambios de estado
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Configura una región live de ARIA para anunciar cambios dinámicos
 * a usuarios de lectores de pantalla.
 */
function initAriaLive() {
  // La región ya está marcada con aria-live="polite" en el HTML
  // Solo necesitamos asegurarnos de que los contadores se actualicen
  const resultsCount = document.getElementById('results-count');
  if (!resultsCount) return;

  // Observer que anuncia cambios en el contador de resultados
  const observer = new MutationObserver(() => {
    // El aria-live="polite" del results-section lo anuncia automáticamente
  });

  observer.observe(resultsCount, { childList: true, subtree: true });
}

// ─────────────────────────────────────────────────────────────────────────────
// Inicialización principal
// ─────────────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  // 1. Partículas de fondo
  initParticles();

  // 2. Health check inicial y cada 30s — pausado cuando el tab está oculto
  checkServerHealth();
  let _healthInterval = setInterval(checkServerHealth, 30_000);

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearInterval(_healthInterval);
    } else {
      checkServerHealth();
      _healthInterval = setInterval(checkServerHealth, 30_000);
    }
  });

  // 3. Efectos de filtros
  initFilterEffects();

  // 4. Ayuda de atajos de teclado
  initKeyboardHelp();

  // 5. Cursor personalizado (solo escritorio)
  initCustomCursor();

  // 6. Aria live regions
  initAriaLive();
});

// Exportar para uso en search.js
export const ui = { showToast };
