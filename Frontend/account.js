/**
 * account.js — Vista de cuenta y gestión de la suscripción
 *
 * Carga los datos del perfil desde Firebase Auth y el estado de la suscripción
 * desde el backend (/api/billing/subscription), que es la fuente de verdad:
 * el plan efectivo, su vigencia y las fechas de compra/vencimiento se calculan
 * server-side. Permite cancelar la facturación mensual.
 */

import { auth } from '/static/firebase.js';
import { onAuthStateChanged } from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js';

// ─── Catálogo de planes (solo para textos descriptivos en la UI) ───────────────
const PLAN_INFO = {
  gratis: { name: 'Gratis', desc: '5 conversiones en total · archivos de hasta 20 MB.' },
  basic:  { name: 'Basic',  desc: '50 conversiones al día · archivos de hasta 40 MB · acceso a The Info Vault.' },
  pro:    { name: 'Pro',    desc: 'Conversiones ilimitadas · archivos de hasta 200 MB · procesamiento prioritario.' },
};

const ESTADO_LABEL = {
  activo:    'Activo',
  cancelado: 'Cancelado',
  expirado:  'Expirado',
  gratis:    'Plan gratis',
};

// ─── Helpers ───────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

function clp(n) {
  if (n === null || n === undefined) return '—';
  return '$' + Math.round(n).toLocaleString('es-CL');
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '—';
  return d.toLocaleDateString('es-CL', { day: 'numeric', month: 'long', year: 'numeric' });
}

function initialOf(text) {
  const t = (text || '').trim();
  return t ? t.charAt(0).toUpperCase() : 'U';
}

function showStatus(message, type) {
  const el = $('ac-status');
  if (!el) return;
  el.textContent = message;
  el.className = 'ac-status ac-status--' + (type || 'success');
  el.hidden = false;
}

function clearStatus() {
  const el = $('ac-status');
  if (el) el.hidden = true;
}

// ─── Render del perfil ──────────────────────────────────────────────────────────
function renderProfile(user) {
  const email = user.email || '';
  const phone = user.phoneNumber || '';
  const displayName = user.displayName || (email ? email.split('@')[0] : '') || 'Tu cuenta';

  $('ac-avatar').textContent = initialOf(displayName || email);
  $('ac-name').textContent   = displayName;
  $('ac-email').textContent  = email || phone || '—';
  $('ac-email-2').textContent = email || '— (cuenta sin correo)';
  $('ac-phone').textContent  = phone || 'No registrado';

  // Firebase Auth expone la fecha de creación de la cuenta.
  const created = user.metadata && user.metadata.creationTime;
  $('ac-since').textContent = created ? fmtDate(created) : '—';
}

// ─── Render de la suscripción ───────────────────────────────────────────────────
function renderSubscription(sub) {
  $('ac-sub-loading').hidden = true;
  $('ac-sub-body').hidden = false;

  const estado = sub.estado || 'gratis';
  const plan   = sub.plan || 'gratis';
  const info   = PLAN_INFO[plan] || PLAN_INFO.gratis;

  // Pill de estado
  const pill = $('ac-state');
  pill.textContent = ESTADO_LABEL[estado] || estado;
  pill.className = 'ac-state-pill ac-state-pill--' + estado;
  pill.hidden = false;

  // ¿Es un plan de pago mensual actualmente vigente? (activo o cancelado-con-acceso)
  const isMonthly = estado === 'activo' || estado === 'cancelado';

  // Nombre, indicador de ciclo y precio
  $('ac-plan-name').textContent = info.name;
  $('ac-plan-cycle').hidden = !isMonthly;        // chip "Mensual" junto al nombre
  $('ac-plan-price').textContent =
    sub.monto_total ? `${clp(sub.monto_total)} CLP / mes` : 'Sin costo';
  $('ac-plan-desc').textContent = info.desc;

  // Fechas: solo si existe una compra registrada
  const dates = $('ac-sub-dates');
  if (sub.fecha_compra) {
    dates.hidden = false;

    // El ciclo de facturación solo aplica mientras el plan de pago siga vigente.
    $('ac-cycle-row').hidden = !isMonthly;

    // Día de compra del plan (siempre que hubo una compra).
    $('ac-buy-date').textContent = fmtDate(sub.fecha_compra);

    // Fecha de la próxima facturación / fin de acceso / vencimiento.
    const vencLabel = $('ac-venc-label');
    if (estado === 'activo')         vencLabel.textContent = 'Próxima facturación';
    else if (estado === 'cancelado') vencLabel.textContent = 'Acceso hasta';
    else                             vencLabel.textContent = 'Venció el';
    $('ac-venc-date').textContent = fmtDate(sub.fecha_vencimiento);
  } else {
    dates.hidden = true;
  }

  // Nota explicativa según estado
  const note = $('ac-renew-note');
  if (estado === 'activo') {
    note.textContent =
      `Tu plan se renueva automáticamente el ${fmtDate(sub.fecha_vencimiento)} ` +
      `por ${clp(sub.monto_total)} CLP. Puedes cancelar cuando quieras, sin costo.`;
  } else if (estado === 'cancelado') {
    note.textContent =
      `Cancelaste la renovación automática. Mantendrás el plan ${info.name} ` +
      `hasta el ${fmtDate(sub.fecha_vencimiento)}; después tu cuenta volverá a Gratis.`;
  } else if (estado === 'expirado') {
    note.textContent =
      `Tu suscripción venció el ${fmtDate(sub.fecha_vencimiento)}. ` +
      `Renueva un plan para recuperar el acceso ampliado.`;
  } else {
    note.textContent =
      'Estás en el plan Gratis. Mejora tu plan para convertir más documentos y acceder a The Info Vault.';
  }

  // Botones
  const upgradeBtn = $('ac-upgrade-btn');
  upgradeBtn.textContent = (estado === 'activo' && plan === 'pro') ? 'Ver planes' : 'Mejorar mi plan';

  const cancelBtn = $('ac-cancel-btn');
  cancelBtn.hidden = estado !== 'activo';
}

function renderSubError() {
  $('ac-sub-loading').hidden = true;
  $('ac-sub-body').hidden = false;
  $('ac-state').hidden = true;
  $('ac-plan-name').textContent = 'No disponible';
  $('ac-plan-cycle').hidden = true;
  $('ac-plan-price').textContent = '';
  $('ac-plan-desc').textContent = '';
  $('ac-sub-dates').hidden = true;
  $('ac-renew-note').textContent =
    'No pudimos cargar tu suscripción en este momento. Recarga la página para reintentar.';
  $('ac-cancel-btn').hidden = true;
}

// ─── API ─────────────────────────────────────────────────────────────────────────
async function authHeaders(user) {
  const idToken = await user.getIdToken(false);
  return {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'Authorization': 'Bearer ' + idToken,
    'ngrok-skip-browser-warning': 'true',
  };
}

async function loadSubscription(user) {
  try {
    const res = await fetch('/api/billing/subscription', {
      method: 'GET',
      headers: await authHeaders(user),
      credentials: 'omit',
    });
    if (!res.ok) { renderSubError(); return; }
    renderSubscription(await res.json());
  } catch (e) {
    renderSubError();
  }
}

async function confirmCancel(user) {
  const confirmBtn = $('cancel-modal-confirm');
  confirmBtn.disabled = true;
  confirmBtn.textContent = 'Cancelando…';
  try {
    const res = await fetch('/api/billing/cancel', {
      method: 'POST',
      headers: await authHeaders(user),
      credentials: 'omit',
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = (data && (typeof data.detail === 'string' ? data.detail : data.detail?.message))
        || 'No se pudo cancelar la suscripción.';
      showStatus(msg, 'error');
    } else {
      renderSubscription(data);
      showStatus(
        `Suscripción cancelada. Conservas el acceso hasta el ${fmtDate(data.fecha_vencimiento)}.`,
        'success'
      );
    }
  } catch (e) {
    showStatus('Sin conexión con el servidor. Intenta nuevamente.', 'error');
  } finally {
    confirmBtn.disabled = false;
    confirmBtn.textContent = 'Sí, cancelar';
    closeModal();
  }
}

// ─── Modal ─────────────────────────────────────────────────────────────────────
function openModal()  { $('cancel-modal').hidden = false; document.body.style.overflow = 'hidden'; }
function closeModal() { $('cancel-modal').hidden = true;  document.body.style.overflow = ''; }

// ─── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  let _user = null;

  onAuthStateChanged(auth, user => {
    // auth-guard.js redirige a /auth si no hay sesión; aquí solo actuamos si la hay.
    if (!user) return;
    _user = user;
    renderProfile(user);
    loadSubscription(user);
  });

  // Acciones del modal de cancelación
  $('ac-cancel-btn').addEventListener('click', () => { clearStatus(); openModal(); });
  $('cancel-modal-dismiss').addEventListener('click', closeModal);
  $('cancel-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeModal();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !$('cancel-modal').hidden) closeModal();
  });
  $('cancel-modal-confirm').addEventListener('click', () => {
    if (_user) confirmCancel(_user);
  });

  // Aviso de bienvenida si se llega recién suscrito desde el checkout
  if (new URLSearchParams(window.location.search).get('subscribed') === '1') {
    history.replaceState({}, '', '/account');
    showStatus('¡Suscripción activada! Aquí tienes el detalle de tu plan.', 'success');
  }
});
