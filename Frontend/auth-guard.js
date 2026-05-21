/**
 * auth-guard.js — Protección de rutas y menú de usuario
 * Cargado como <script type="module" src="/static/auth-guard.js"> — se auto-inicializa.
 * Redirige a /auth si el usuario no está autenticado.
 * Nunca bloquea la página más de AUTH_TIMEOUT_MS milisegundos.
 */

import { auth } from '/static/firebase.js';
import { onAuthStateChanged, signOut } from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js';

const AUTH_TIMEOUT_MS = 6000;

function _esc(str) {
  return String(str).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  );
}

function _displayName(user) {
  return user.displayName || user.email || user.phoneNumber || 'Usuario';
}

function _removeOverlay() {
  document.getElementById('auth-loading')?.remove();
}

function _redirectToAuth() {
  if (window.location.pathname !== '/auth') {
    window.location.replace('/auth');
  }
}

// Si Firebase no responde en AUTH_TIMEOUT_MS, no bloqueamos la página indefinidamente.
const _timeout = setTimeout(() => {
  _removeOverlay();
  if (document.body.dataset.public !== 'true') _redirectToAuth();
}, AUTH_TIMEOUT_MS);

const _isPublicPage = document.body.dataset.public === 'true';

onAuthStateChanged(auth, user => {
  clearTimeout(_timeout);
  _removeOverlay();

  if (!user) {
    if (!_isPublicPage) _redirectToAuth();
    return;
  }

  // Usuario autenticado: mostrar menú con nombre y botón de salida
  const menu = document.getElementById('user-menu');
  if (!menu) return;

  const name = _esc(_displayName(user));
  menu.innerHTML = `
    <span class="user-display-name" title="${name}">${name}</span>
    <button id="logout-btn" class="footer-logout-btn" aria-label="Cerrar sesión">Salir</button>
  `;

  document.getElementById('logout-btn').addEventListener('click', async () => {
    await signOut(auth);
    window.location.replace('/auth');
  });
});
