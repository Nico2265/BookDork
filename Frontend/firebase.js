import { initializeApp } from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js';
import { getAuth } from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js';
import { getFirestore } from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js';

const _config = {
  apiKey:            'AIzaSyDNTjRu-33BRNKo7A39Kbkk9_2U9kI1kjo',
  authDomain:        'bookdork-b9825.firebaseapp.com',
  projectId:         'bookdork-b9825',
  storageBucket:     'bookdork-b9825.firebasestorage.app',
  messagingSenderId: '318915320884',
  appId:             '1:318915320884:web:dde60fd148612f5740be61',
};

const _app = initializeApp(_config);
export const auth = getAuth(_app);
export const db   = getFirestore(_app);
