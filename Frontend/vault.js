/**
 * vault.js — The Info Vault
 * Carga libros convertidos desde /api/vault/books, filtra por tema/año/edición/período,
 * y permite descargar el Markdown via /api/vault/download/{book_id}.
 */

import { auth } from '/static/firebase.js';
import { onAuthStateChanged } from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js';

// ─── Estado ───────────────────────────────────────────────────────────────────

const state = {
  topic:   '',
  year:    '',
  edition: '',
  period:  '',
  offset:  0,
  limit:   24,
  total:   0,
  loading: false,
};

let _idToken = null;

// ─── Mapas de etiquetas ───────────────────────────────────────────────────────

// Topic names match conversion_cache.py classify_topic() output (Spanish keys)
const TOPIC_LABELS = {
  ia:           'IA',
  programacion: 'Programación',
  matematicas:  'Matemáticas',
  fisica:       'Física',
  economia:     'Economía',
  biologia:     'Biología',
  historia:     'Historia',
  psicologia:   'Psicología',
  filosofia:    'Filosofía',
  general:      'General',
};

const PERIOD_LABELS = {
  '': 'en total', today: 'hoy', week: 'esta semana',
  biweek: 'últimas 2 semanas', month: 'este mes',
};

// Gradientes según tema del libro (keyed by Spanish topic names)
const TOPIC_GRADIENTS = {
  ia:           'linear-gradient(135deg,#1a1030 0%,#2d1b69 55%,#1a0a3a 100%)',
  fisica:       'linear-gradient(135deg,#090918 0%,#10103a 55%,#090918 100%)',
  matematicas:  'linear-gradient(135deg,#100a00 0%,#241800 55%,#100a00 100%)',
  programacion: 'linear-gradient(135deg,#001a10 0%,#003020 55%,#001a10 100%)',
  economia:     'linear-gradient(135deg,#1a1000 0%,#2d1a00 55%,#1a1000 100%)',
  biologia:     'linear-gradient(135deg,#001a08 0%,#003010 55%,#001a08 100%)',
  historia:     'linear-gradient(135deg,#150d00 0%,#2a1a00 55%,#150d00 100%)',
  psicologia:   'linear-gradient(135deg,#001520 0%,#002a40 55%,#001520 100%)',
  filosofia:    'linear-gradient(135deg,#0a0020 0%,#180038 55%,#0a0020 100%)',
  general:      'linear-gradient(135deg,#0f0f0f 0%,#1e1e1e 55%,#0f0f0f 100%)',
};

// ─── DOM helpers ──────────────────────────────────────────────────────────────

const $ = id => document.getElementById(id);

function showToast(msg, type = 'info') {
  let c = document.getElementById('toast-container');
  if (!c) {
    c = Object.assign(document.createElement('div'), { id: 'toast-container' });
    c.setAttribute('aria-live', 'polite');
    document.body.appendChild(c);
  }
  const t = Object.assign(document.createElement('div'), {
    className: `toast toast--${type}`, role: 'alert', textContent: msg,
  });
  c.appendChild(t);
  setTimeout(() => {
    t.classList.add('is-hiding');
    t.addEventListener('animationend', () => t.remove(), { once: true });
  }, 3500);
}

// ─── Autenticación y acceso ───────────────────────────────────────────────────

async function getToken(forceRefresh = false) {
  const user = auth.currentUser;
  if (!user) return null;
  _idToken = await user.getIdToken(forceRefresh);
  return _idToken;
}

async function checkPlanAccess() {
  const token = await getToken();
  if (!token) return false;

  try {
    const res = await fetch('/api/vault/books?limit=1&offset=0', {
      headers: { Authorization: `Bearer ${token}`, 'ngrok-skip-browser-warning': 'true' },
    });
    if (res.status === 403) return false;
    return res.ok;
  } catch {
    return false;
  }
}

// ─── API ──────────────────────────────────────────────────────────────────────

async function fetchBooks(append = false) {
  if (state.loading) return;
  state.loading = true;

  const token = await getToken();
  if (!token) { state.loading = false; return; }

  showSkeletons(!append);

  const params = new URLSearchParams({ limit: state.limit, offset: state.offset });
  if (state.topic)   params.set('topic',   state.topic);
  if (state.year)    params.set('year',    state.year);
  if (state.edition) params.set('edition', state.edition);
  if (state.period)  params.set('period',  state.period);

  try {
    const res = await fetch(`/api/vault/books?${params}`, {
      headers: { Authorization: `Bearer ${token}`, 'ngrok-skip-browser-warning': 'true' },
    });

    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data?.detail?.message || data?.detail || `HTTP ${res.status}`);
    }

    const data = await res.json();
    state.total = data.total ?? 0;

    hideSkeletons();
    renderBooks(data.books || [], append);
    updateStatsBar();
    updatePagination();
  } catch (err) {
    hideSkeletons();
    showError(err.message);
  } finally {
    state.loading = false;
  }
}

async function downloadBook(bookId, title, btn) {
  const token = await getToken();
  if (!token) return;

  btn.disabled = true;
  showDownloadModal(title);
  setDownloadProgress(20, 'Conectando al Vault…');

  try {
    setDownloadProgress(50, 'Descargando Markdown…');
    const res = await fetch(`/api/vault/download/${encodeURIComponent(bookId)}`, {
      headers: { Authorization: `Bearer ${token}`, 'ngrok-skip-browser-warning': 'true' },
    });

    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d?.detail || `HTTP ${res.status}`);
    }

    const data = await res.json();
    setDownloadProgress(90, 'Preparando archivo…');

    const blob = new Blob([data.markdown], { type: 'text/markdown;charset=utf-8' });
    const url  = URL.createObjectURL(blob);
    const a    = Object.assign(document.createElement('a'), {
      href: url, download: data.filename, style: 'display:none',
    });
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { document.body.removeChild(a); URL.revokeObjectURL(url); }, 150);

    setDownloadProgress(100, '¡Descargado!');
    setTimeout(hideDownloadModal, 1000);
    showToast(`"${data.filename}" descargado correctamente`, 'success');
  } catch (err) {
    hideDownloadModal();
    showToast(`Error al descargar: ${err.message}`, 'error');
  } finally {
    btn.disabled = false;
  }
}

// ─── Renderizado ──────────────────────────────────────────────────────────────

function renderBooks(books, append) {
  const grid    = $('vlt-grid');
  const emptyEl = $('vlt-empty');
  const errEl   = $('vlt-error');
  if (!grid) return;

  if (errEl) errEl.hidden = true;

  if (!append) grid.innerHTML = '';

  if (books.length === 0 && !append) {
    if (emptyEl) emptyEl.hidden = false;
    return;
  }
  if (emptyEl) emptyEl.hidden = true;

  const maxDl = books.length > 0 ? (books[0].download_count || 0) : 0;

  books.forEach((book, idx) => {
    const card = buildCard(book, maxDl, state.offset + idx + 1);
    card.style.animationDelay = `${Math.min(idx * 40, 300)}ms`;
    grid.appendChild(card);
  });
}

function buildCard(book, maxDl, rank) {
  const dlCount = book.download_count || 0;
  const barPct  = maxDl > 0 ? Math.round((dlCount / maxDl) * 100) : 0;
  const topic   = book.topic || 'other';
  const gradient = TOPIC_GRADIENTS[topic] || TOPIC_GRADIENTS.general;
  const topicLabel = TOPIC_LABELS[topic] || topic;

  const edition = book.edition ? (book.edition === 'N/A' ? 'Ed. N/A' : `${book.edition}ª ed.`) : null;
  const coverLetter = (book.title || book.filename || '?')[0].toUpperCase();
  const coverUrl  = book.cover_url || (book.isbn ? `https://covers.openlibrary.org/b/isbn/${book.isbn}-M.jpg` : '');

  const article = document.createElement('article');
  article.className = 'vlt-card';
  article.setAttribute('role', 'listitem');
  article.setAttribute('aria-label', book.title || book.filename);

  article.innerHTML = `
    <div class="vlt-card__cover${coverUrl ? ' vlt-card__cover--has-img' : ''}" style="background:${gradient}">
      ${coverUrl ? `<img class="vlt-card__cover-img" src="${escHtml(coverUrl)}" alt="Portada" loading="eager" decoding="async" data-isbn="${escHtml(book.isbn||'')}" onerror="var fb='https://covers.openlibrary.org/b/isbn/'+this.dataset.isbn+'-M.jpg';if(this.dataset.isbn&&this.src!==fb){this.src=fb}else{this.style.display='none'}">` : ''}
      <span class="vlt-card__rank" aria-label="Posición ${rank}">#${rank}</span>
      ${coverUrl ? '' : `<span class="vlt-card__cover-letter" aria-hidden="true">${coverLetter}</span>`}
      <span class="vlt-card__dl-badge" title="${dlCount.toLocaleString('es')} descargas ${PERIOD_LABELS[state.period] || ''}">
        <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
          <polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
        </svg>
        ${dlCount.toLocaleString('es')}
      </span>
    </div>
    <div class="vlt-card__body">
      <h3 class="vlt-card__title">${escHtml(book.title || book.filename || 'Sin título')}</h3>
      <p class="vlt-card__author">${escHtml(book.author || 'Autor desconocido')}</p>
      <div class="vlt-card__meta">
        ${book.year ? `<span class="vlt-card__year">${book.year}</span>` : ''}
        ${edition   ? `<span class="vlt-card__edition">${escHtml(edition)}</span>` : ''}
        <span class="vlt-card__topic">${escHtml(topicLabel)}</span>
      </div>
    </div>
    <div class="vlt-card__footer">
      <div class="vlt-card__bar-wrap">
        <div class="vlt-card__bar-track">
          <div class="vlt-card__bar-fill" style="width:${barPct}%"></div>
        </div>
        <span class="vlt-card__bar-num">${dlCount.toLocaleString('es')}</span>
      </div>
      <button class="vlt-card__dl-btn" type="button"
              data-book-id="${escHtml(book.book_id)}"
              data-title="${escHtml(book.title || book.filename || book.book_id)}"
              aria-label="Descargar ${escHtml(book.title || book.filename)}">
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
          <polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
        </svg>
        .md
      </button>
    </div>`;

  article.querySelector('.vlt-card__dl-btn').addEventListener('click', e => {
    const btn = e.currentTarget;
    downloadBook(btn.dataset.bookId, btn.dataset.title, btn);
  });

  return article;
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ─── Skeleton / empty / error ─────────────────────────────────────────────────

function showSkeletons(replace) {
  const sk = $('vlt-skeletons');
  if (sk) sk.hidden = false;
  if (replace) {
    const g = $('vlt-grid');
    if (g) g.innerHTML = '';
    const e = $('vlt-empty');
    if (e) e.hidden = true;
  }
}

function hideSkeletons() {
  const sk = $('vlt-skeletons');
  if (sk) sk.hidden = true;
}

function showError(msg) {
  const el = $('vlt-error');
  const msgEl = $('vlt-error-msg');
  if (el) el.hidden = false;
  if (msgEl) msgEl.textContent = msg || 'Error al cargar el Vault.';
}

// ─── Stats bar y paginación ───────────────────────────────────────────────────

function updateStatsBar() {
  const label = $('vlt-count-label');
  const totalEl = $('vlt-total-books');
  if (!label) return;
  const pLabel = PERIOD_LABELS[state.period] || 'en total';
  label.textContent = state.total
    ? `${state.total.toLocaleString('es')} libro${state.total !== 1 ? 's' : ''} · más descargados ${pLabel}`
    : 'Sin resultados';
  if (totalEl) totalEl.textContent = state.total.toLocaleString('es');
}

function updatePagination() {
  const pag = $('vlt-pagination');
  if (!pag) return;
  const hasMore = state.offset < state.total;
  pag.hidden = !hasMore;
}

// ─── Filtros ──────────────────────────────────────────────────────────────────

function resetAndFetch() {
  state.offset = 0;
  fetchBooks(false);
}

function initFilters() {
  // Período
  document.querySelectorAll('.vlt-period-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.vlt-period-btn').forEach(b => {
        b.classList.remove('vlt-period-btn--active');
        b.setAttribute('aria-pressed', 'false');
      });
      btn.classList.add('vlt-period-btn--active');
      btn.setAttribute('aria-pressed', 'true');
      state.period = btn.dataset.period;
      resetAndFetch();
    });
  });

  // Año
  $('vlt-year-select')?.addEventListener('change', e => {
    state.year = e.target.value;
    resetAndFetch();
  });

  // Edición
  $('vlt-edition-select')?.addEventListener('change', e => {
    state.edition = e.target.value;
    resetAndFetch();
  });

  // Tema chips
  document.querySelectorAll('.vlt-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      document.querySelectorAll('.vlt-chip').forEach(c => c.classList.remove('vlt-chip--active'));
      chip.classList.add('vlt-chip--active');
      state.topic = chip.dataset.topic;
      resetAndFetch();
    });
  });

  // Reset btn
  $('vlt-reset-btn')?.addEventListener('click', () => {
    state.topic = ''; state.year = ''; state.edition = ''; state.period = '';
    document.querySelectorAll('.vlt-period-btn').forEach(b => {
      b.classList.remove('vlt-period-btn--active');
      b.setAttribute('aria-pressed', 'false');
    });
    document.querySelector('[data-period=""]')?.classList.add('vlt-period-btn--active');
    document.querySelector('[data-period=""]')?.setAttribute('aria-pressed', 'true');
    document.querySelectorAll('.vlt-chip').forEach(c => c.classList.remove('vlt-chip--active'));
    document.querySelector('[data-topic=""]')?.classList.add('vlt-chip--active');
    const ys = $('vlt-year-select'); if (ys) ys.value = '';
    const es = $('vlt-edition-select'); if (es) es.value = '';
    resetAndFetch();
  });

  // Retry btn
  $('vlt-retry-btn')?.addEventListener('click', () => fetchBooks(false));

  // Load more
  $('vlt-load-more')?.addEventListener('click', () => {
    state.offset += state.limit;
    fetchBooks(true);
  });
}

// ─── Modal de descarga ────────────────────────────────────────────────────────

function showDownloadModal(title) {
  const modal = $('vlt-dl-modal');
  const bookEl = $('vlt-dl-modal-book');
  if (modal) modal.hidden = false;
  if (bookEl) bookEl.textContent = title || '';
}

function hideDownloadModal() {
  const modal = $('vlt-dl-modal');
  if (modal) modal.hidden = true;
  setDownloadProgress(0, 'Preparando…');
}

function setDownloadProgress(pct, status) {
  const fill = $('vlt-dl-progress');
  const st   = $('vlt-dl-status');
  if (fill) fill.style.width = `${pct}%`;
  if (st)   st.textContent   = status;
}

// ─── Bootstrap ───────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  $('vlt-dl-modal-close')?.addEventListener('click', hideDownloadModal);
  $('vlt-dl-modal')?.addEventListener('click', e => {
    if (e.target === e.currentTarget) hideDownloadModal();
  });

  initFilters();

  onAuthStateChanged(auth, async user => {
    if (!user) return; // auth-guard.js maneja el redirect

    const allowed = await checkPlanAccess();

    if (!allowed) {
      $('vlt-paywall').hidden  = false;
      $('vlt-content').hidden  = true;
    } else {
      $('vlt-paywall').hidden  = true;
      $('vlt-content').hidden  = false;
      fetchBooks(false);
    }
  });
});
