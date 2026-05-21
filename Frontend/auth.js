/**
 * auth.js — Lógica de autenticación Firebase para BookDork
 *
 * Métodos:
 *   · Correo electrónico + contraseña (login / registro con verificación de teléfono)
 *   · Teléfono + OTP SMS (login / registro automático)
 */

import { auth, db } from '/static/firebase.js';
import {
  onAuthStateChanged,
  createUserWithEmailAndPassword,
  signInWithEmailAndPassword,
  signInWithPhoneNumber,
  RecaptchaVerifier,
  PhoneAuthProvider,
  linkWithCredential,
} from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js';
import {
  doc, getDoc, setDoc, serverTimestamp,
} from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js';

// ─── Destino de redirección post-auth ─────────────────────────────────────────

function _getRedirectTarget() {
  const next = new URLSearchParams(window.location.search).get('next') || '';
  return (next && next.startsWith('/') && !next.startsWith('//')) ? next : '/search';
}

// ─── Estado global ─────────────────────────────────────────────────────────────

let _recaptcha         = null;
let _confirmResult     = null;
let _pendingEmail      = '';
let _pendingPassword   = '';
let _pendingPhone      = '';
let _emailMode         = 'login';   // 'login' | 'register'

// ─── Helpers ───────────────────────────────────────────────────────────────────

function normalizePhone(prefix, number) {
  const p = prefix.trim().replace(/\s/g, '');
  const n = number.trim().replace(/[^0-9]/g, '');
  return `${p}${n}`;
}

function showMsg(id, text, type) {
  const el = document.getElementById(id);
  if (!el) return;
  el.hidden     = false;
  el.textContent = text;
  el.className   = `auth-msg--${type}`;
}

function clearMsg(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.hidden     = true;
  el.textContent = '';
  el.className   = '';
}

function setBusy(btnId, busy, originalText) {
  const btn = document.getElementById(btnId);
  if (!btn) return;
  btn.disabled = busy;
  btn.innerHTML = busy
    ? `<span class="auth-spinner"></span> ${originalText}…`
    : originalText;
}

function getRecaptcha() {
  if (!_recaptcha) {
    _recaptcha = new RecaptchaVerifier(auth, 'recaptcha-container', {
      size: 'invisible',
      'expired-callback': () => { _recaptcha = null; },
    });
  }
  return _recaptcha;
}

// ─── Firestore: guardar usuario nuevo ──────────────────────────────────────────

async function saveNewUser(uid, email, phoneNumber) {
  await setDoc(doc(db, 'usuarios', uid), {
    email:               email || null,
    phoneNumber:         phoneNumber || null,
    plan:                'gratis',
    conversiones:        0,
    conversionesDiarias: 0,
    fechaConversiones:   '',
    stripeCustomerId:    null,
    creadoEn:            serverTimestamp(),
  });

  if (phoneNumber) {
    await setDoc(doc(db, 'phones', phoneNumber), {
      uid,
      creadoEn: serverTimestamp(),
    });
  }
}

// ─── Verificar unicidad del teléfono ───────────────────────────────────────────

async function isPhoneTaken(normalizedPhone) {
  const snap = await getDoc(doc(db, 'phones', normalizedPhone));
  return snap.exists();
}

// ─── Verificar si ya existe el documento del usuario ───────────────────────────

async function getUserDoc(uid) {
  return getDoc(doc(db, 'usuarios', uid));
}

// ─── Errores Firebase → mensajes en español ────────────────────────────────────

function friendlyError(code) {
  const map = {
    'auth/email-already-in-use':      'Este correo ya está registrado.',
    'auth/invalid-email':             'Correo electrónico no válido.',
    'auth/weak-password':             'Contraseña demasiado débil (mínimo 6 caracteres).',
    'auth/wrong-password':            'Contraseña incorrecta.',
    'auth/user-not-found':            'No existe cuenta con ese correo.',
    'auth/invalid-credential':        'Correo o contraseña incorrectos.',
    'auth/too-many-requests':         'Demasiados intentos. Espera unos minutos e inténtalo de nuevo.',
    'auth/invalid-phone-number':      'Número de teléfono no válido. Incluye el código de país (+XX).',
    'auth/invalid-verification-code': 'Código incorrecto. Inténtalo de nuevo.',
    'auth/code-expired':              'El código expiró. Solicita uno nuevo.',
    'auth/credential-already-in-use': 'Este teléfono ya está vinculado a otra cuenta.',
    'auth/network-request-failed':    'Sin conexión a internet.',
    'auth/operation-not-allowed':     'Método de inicio de sesión no habilitado.',
    'permission-denied':              'Permisos insuficientes. Contacta al administrador.',
  };
  return map[code] || `Error inesperado (${code}).`;
}

// ─── Flujo: LOGIN con correo ────────────────────────────────────────────────────

async function handleEmailLogin() {
  const email    = document.getElementById('email-input').value.trim();
  const password = document.getElementById('password-input').value;
  clearMsg('email-msg');

  if (!email || !password) {
    showMsg('email-msg', 'Completa correo y contraseña.', 'error'); return;
  }

  setBusy('email-login-btn', true, 'Continuar');
  try {
    await signInWithEmailAndPassword(auth, email, password);
    window.location.replace(_getRedirectTarget());
  } catch (e) {
    showMsg('email-msg', friendlyError(e.code), 'error');
  } finally {
    setBusy('email-login-btn', false, 'Continuar');
  }
}

// ─── Flujo: REGISTRO con correo (paso 1 — enviar OTP al teléfono) ──────────────

async function handleSendOtpForEmail() {
  const email    = document.getElementById('email-input').value.trim();
  const password = document.getElementById('password-input').value;
  const prefix   = document.getElementById('email-phone-prefix').value.trim();
  const number   = document.getElementById('email-phone-number').value.trim();
  clearMsg('email-msg');

  if (!email || !password) {
    showMsg('email-msg', 'Completa el correo y la contraseña.', 'error'); return;
  }
  if (password.length < 6) {
    showMsg('email-msg', 'La contraseña debe tener al menos 6 caracteres.', 'error'); return;
  }
  if (!prefix || !number) {
    showMsg('email-msg', 'Ingresa el número de teléfono completo.', 'error'); return;
  }

  const phone = normalizePhone(prefix, number);
  setBusy('send-otp-email-btn', true, 'Enviar código de verificación');

  try {
    const taken = await isPhoneTaken(phone);
    if (taken) {
      showMsg('email-msg', 'Este número ya está registrado en otra cuenta.', 'error');
      return;
    }

    _pendingEmail    = email;
    _pendingPassword = password;
    _pendingPhone    = phone;

    _confirmResult = await signInWithPhoneNumber(auth, phone, getRecaptcha());

    document.getElementById('email-otp-phone-display').textContent = phone;
    document.getElementById('email-step-1').hidden = true;
    document.getElementById('email-step-2').hidden = false;

  } catch (e) {
    if (e.code === 'auth/argument-error' || e.code === 'auth/captcha-check-failed') {
      _recaptcha = null;
    }
    showMsg('email-msg', friendlyError(e.code), 'error');
  } finally {
    setBusy('send-otp-email-btn', false, 'Enviar código de verificación');
  }
}

// ─── Flujo: REGISTRO con correo (paso 2 — verificar OTP + crear cuenta) ─────────

async function handleEmailRegister() {
  const code = document.getElementById('email-otp-input').value.trim();
  clearMsg('email-msg');

  if (!code || code.length !== 6) {
    showMsg('email-msg', 'Ingresa el código de 6 cifras.', 'error'); return;
  }

  setBusy('email-register-btn', true, 'Crear cuenta');
  try {
    const phoneCred = PhoneAuthProvider.credential(_confirmResult.verificationId, code);
    const userCred  = await createUserWithEmailAndPassword(auth, _pendingEmail, _pendingPassword);

    try {
      await linkWithCredential(userCred.user, phoneCred);
    } catch (linkErr) {
      await userCred.user.delete();
      showMsg('email-msg', friendlyError(linkErr.code), 'error');
      return;
    }

    try {
      await saveNewUser(userCred.user.uid, _pendingEmail, _pendingPhone);
    } catch (dbErr) {
      console.warn('saveNewUser:', dbErr.code || dbErr);
    }
    window.location.replace(_getRedirectTarget());

  } catch (e) {
    showMsg('email-msg', friendlyError(e.code), 'error');
  } finally {
    setBusy('email-register-btn', false, 'Crear cuenta');
  }
}

// ─── Flujo: LOGIN/REGISTRO con teléfono (paso 1 — enviar OTP) ─────────────────

async function handleSendPhoneOtp() {
  const prefix = document.getElementById('phone-prefix').value.trim();
  const number = document.getElementById('phone-number').value.trim();
  clearMsg('phone-msg');

  if (!prefix || !number) {
    showMsg('phone-msg', 'Ingresa el número de teléfono completo.', 'error'); return;
  }

  const phone = normalizePhone(prefix, number);
  setBusy('send-phone-otp-btn', true, 'Enviar código SMS');

  try {
    _confirmResult = await signInWithPhoneNumber(auth, phone, getRecaptcha());
    _pendingPhone  = phone;

    document.getElementById('phone-otp-display').textContent = phone;
    document.getElementById('phone-step-1').hidden = true;
    document.getElementById('phone-step-2').hidden = false;

  } catch (e) {
    if (e.code === 'auth/argument-error' || e.code === 'auth/captcha-check-failed') {
      _recaptcha = null;
    }
    showMsg('phone-msg', friendlyError(e.code), 'error');
  } finally {
    setBusy('send-phone-otp-btn', false, 'Enviar código SMS');
  }
}

// ─── Flujo: LOGIN/REGISTRO con teléfono (paso 2 — verificar OTP) ───────────────

async function handleVerifyPhone() {
  const code = document.getElementById('phone-otp-input').value.trim();
  clearMsg('phone-msg');

  if (!code || code.length !== 6) {
    showMsg('phone-msg', 'Ingresa el código de 6 cifras.', 'error'); return;
  }

  setBusy('verify-phone-btn', true, 'Verificar y acceder');
  try {
    const result = await _confirmResult.confirm(code);
    const user   = result.user;

    const userSnap = await getUserDoc(user.uid);
    if (!userSnap.exists()) {
      try {
        await saveNewUser(user.uid, user.email, _pendingPhone);
      } catch (dbErr) {
        console.warn('saveNewUser:', dbErr.code || dbErr);
      }
    }
    window.location.replace(_getRedirectTarget());

  } catch (e) {
    showMsg('phone-msg', friendlyError(e.code), 'error');
  } finally {
    setBusy('verify-phone-btn', false, 'Verificar y acceder');
  }
}

// ─── UI: cambio de método (Correo / Teléfono) ──────────────────────────────────

function activateTab(tabId) {
  const isEmail = tabId === 'tab-email';
  document.getElementById('tab-email').classList.toggle('active', isEmail);
  document.getElementById('tab-phone').classList.toggle('active', !isEmail);
  document.getElementById('tab-email').setAttribute('aria-selected', String(isEmail));
  document.getElementById('tab-phone').setAttribute('aria-selected', String(!isEmail));
  document.getElementById('panel-email').hidden = !isEmail;
  document.getElementById('panel-phone').hidden = isEmail;
  clearMsg('email-msg');
  clearMsg('phone-msg');
}

// ─── UI: cambio modo login / register ──────────────────────────────────────────

function setEmailMode(mode) {
  _emailMode = mode;
  const isRegister = mode === 'register';

  document.getElementById('btn-login').classList.toggle('active', !isRegister);
  document.getElementById('btn-register').classList.toggle('active', isRegister);
  document.getElementById('btn-login').setAttribute('aria-selected', String(!isRegister));
  document.getElementById('btn-register').setAttribute('aria-selected', String(isRegister));

  document.getElementById('auth-form-title').textContent = isRegister
    ? 'Crear cuenta' : 'Iniciar sesión';
  document.getElementById('auth-form-sub').textContent = isRegister
    ? 'Únete a BookDork' : 'Bienvenido de nuevo a BookDork';

  document.getElementById('phone-section-email').hidden = !isRegister;
  document.getElementById('login-section-email').hidden = isRegister;

  document.getElementById('email-step-1').hidden = false;
  document.getElementById('email-step-2').hidden = true;
  clearMsg('email-msg');

  const pwInput = document.getElementById('password-input');
  pwInput.autocomplete = isRegister ? 'new-password' : 'current-password';

  // Resetear también el panel teléfono si estaba en paso 2
  document.getElementById('phone-step-1').hidden = false;
  document.getElementById('phone-step-2').hidden = true;
  clearMsg('phone-msg');
}

// ─── UI: mostrar/ocultar contraseña ────────────────────────────────────────────

function togglePasswordVisibility() {
  const input = document.getElementById('password-input');
  const icon  = document.getElementById('eye-icon');
  const isText = input.type === 'text';
  input.type = isText ? 'password' : 'text';
  icon.innerHTML = isText
    ? '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>'
    : '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/>'
      + '<path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>'
      + '<line x1="1" y1="1" x2="23" y2="23"/>';
}

// ─── Init ──────────────────────────────────────────────────────────────────────

function init() {
  onAuthStateChanged(auth, user => {
    if (user) { window.location.replace(_getRedirectTarget()); }
  });

  // Modo principal
  document.getElementById('btn-login').addEventListener('click', () => setEmailMode('login'));
  document.getElementById('btn-register').addEventListener('click', () => setEmailMode('register'));

  // Método (Correo / Teléfono)
  document.getElementById('tab-email').addEventListener('click', () => activateTab('tab-email'));
  document.getElementById('tab-phone').addEventListener('click', () => activateTab('tab-phone'));

  // Mostrar/ocultar contraseña
  document.getElementById('toggle-password').addEventListener('click', togglePasswordVisibility);

  // Acciones email
  document.getElementById('email-login-btn').addEventListener('click', handleEmailLogin);
  document.getElementById('send-otp-email-btn').addEventListener('click', handleSendOtpForEmail);
  document.getElementById('email-register-btn').addEventListener('click', handleEmailRegister);
  document.getElementById('email-otp-back-btn').addEventListener('click', () => {
    document.getElementById('email-step-1').hidden = false;
    document.getElementById('email-step-2').hidden = true;
    clearMsg('email-msg');
  });

  // Acciones teléfono
  document.getElementById('send-phone-otp-btn').addEventListener('click', handleSendPhoneOtp);
  document.getElementById('verify-phone-btn').addEventListener('click', handleVerifyPhone);
  document.getElementById('phone-otp-back-btn').addEventListener('click', () => {
    document.getElementById('phone-step-1').hidden = false;
    document.getElementById('phone-step-2').hidden = true;
    clearMsg('phone-msg');
  });

  // Enter para enviar
  document.getElementById('password-input').addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    _emailMode === 'login' ? handleEmailLogin() : handleSendOtpForEmail();
  });
  document.getElementById('email-otp-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') handleEmailRegister();
  });
  document.getElementById('phone-otp-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') handleVerifyPhone();
  });
}

document.addEventListener('DOMContentLoaded', init);
