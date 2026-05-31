import { initializeApp } from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js';
import { getAuth } from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js';
import { getFirestore } from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js';
import { initializeAppCheck, ReCaptchaEnterpriseProvider } from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-app-check.js';

const _config = {
  apiKey:            'AIzaSyDNTjRu-33BRNKo7A39Kbkk9_2U9kI1kjo',
  authDomain:        'bookdork-b9825.firebaseapp.com',
  projectId:         'bookdork-b9825',
  storageBucket:     'bookdork-b9825.firebasestorage.app',
  messagingSenderId: '318915320884',
  appId:             '1:318915320884:web:dde60fd148612f5740be61',
};

// ─── App Check (reCAPTCHA Enterprise) ────────────────────────────────────────
// Verifica criptográficamente que los requests provienen de esta app real.
// Activa en Firebase Console → App Check ANTES de pasar a enforcement.
// REEMPLAZA '<RECAPTCHA_ENTERPRISE_SITE_KEY>' con tu site key.
// Obtén la site key en: https://console.firebase.google.com/project/bookdork-b9825/appcheck
const _RECAPTCHA_SITE_KEY = '<RECAPTCHA_ENTERPRISE_SITE_KEY>';

const _app = initializeApp(_config);

if (_RECAPTCHA_SITE_KEY !== '<RECAPTCHA_ENTERPRISE_SITE_KEY>') {
  initializeAppCheck(_app, {
    provider: new ReCaptchaEnterpriseProvider(_RECAPTCHA_SITE_KEY),
    isTokenAutoRefreshEnabled: true,
  });
}

export const auth = getAuth(_app);
export const db   = getFirestore(_app);
