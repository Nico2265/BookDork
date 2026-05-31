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
  query:   '',          // texto de búsqueda (cliente-side)
  offset:  0,
  limit:   24,
  total:   0,           // total reportado por el server tras filtros
  loaded:  [],          // libros acumulados desde el server (paginados)
  loading: false,
};

let _idToken = null;
let _searchDebounce = null;

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
    const fresh = data.books || [];
    state.loaded = append ? state.loaded.concat(fresh) : fresh;

    hideSkeletons();
    populateYearOptions();
    renderFiltered();
    updateStatsBar();
    updatePagination();
  } catch (err) {
    hideSkeletons();
    showError(err.message);
  } finally {
    state.loading = false;
  }
}

// Filtro cliente-side (búsqueda libre sobre los libros cargados).
function matchesQuery(book, qNorm) {
  if (!qNorm) return true;
  const fields = [
    book.title, book.filename, book.author, book.isbn,
    book.topic, book.edition,
    book.year != null ? String(book.year) : '',
  ];
  for (const f of fields) {
    if (f && normalize(String(f)).includes(qNorm)) return true;
  }
  return false;
}

// Quita diacríticos y normaliza a minúsculas para comparar de forma robusta.
// El rango ̀-ͯ cubre los "combining diacritical marks" del bloque NFD.
const DIACRITICS_RE = /[̀-ͯ]/g;
function normalize(s) {
  return s.normalize('NFD').replace(DIACRITICS_RE, '').toLowerCase().trim();
}

function getFilteredBooks() {
  const q = normalize(state.query);
  if (!q) return state.loaded;
  return state.loaded.filter(b => matchesQuery(b, q));
}

function renderFiltered() {
  const list = getFilteredBooks();
  renderBooks(list, false);
  updateStatsBar();
  updateActiveFilters();
}

// Pobla el <select> de año con los años distintos vistos en los libros cargados.
// Preserva la selección actual si sigue siendo válida.
function populateYearOptions() {
  const sel = $('vlt-year-select');
  if (!sel) return;

  const years = new Set();
  for (const b of state.loaded) {
    const y = parseInt(b.year, 10);
    if (Number.isFinite(y) && y >= 1500 && y <= 2100) years.add(y);
  }
  const sorted = Array.from(years).sort((a, b) => b - a);

  const current = state.year;
  // Reset y reconstruye
  sel.replaceChildren();
  const optAll = document.createElement('option');
  optAll.value = '';
  optAll.textContent = 'Todos los años';
  sel.appendChild(optAll);

  for (const y of sorted) {
    const opt = document.createElement('option');
    opt.value = String(y);
    opt.textContent = String(y);
    sel.appendChild(opt);
  }

  // Restaura selección si seguía vigente
  if (current && sorted.map(String).includes(String(current))) {
    sel.value = String(current);
  } else if (current) {
    // El año previo ya no está en la lista — añádelo para no perder la selección
    const opt = document.createElement('option');
    opt.value = String(current);
    opt.textContent = String(current);
    sel.appendChild(opt);
    sel.value = String(current);
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
    if (emptyEl) {
      emptyEl.hidden = false;
      const titleEl = $('vlt-empty-title');
      const subEl   = $('vlt-empty-sub');
      if (state.query && titleEl && subEl) {
        titleEl.textContent = 'Sin coincidencias';
        subEl.textContent = `No encontramos libros que coincidan con "${state.query}" en los ${state.loaded.length} libros cargados.`;
      } else if (titleEl && subEl) {
        titleEl.textContent = 'Vault vacío';
        subEl.replaceChildren(
          document.createTextNode('No hay libros con estos filtros todavía.'),
          document.createElement('br'),
          document.createTextNode('Sé el primero en convertir y contribuir.'),
        );
      }
    }
    return;
  }
  if (emptyEl) emptyEl.hidden = true;

  // El rank refleja la posición global por descargas dentro del conjunto cargado,
  // no la posición filtrada — así #1 sigue siendo el más descargado real.
  const baseList = state.loaded;
  const rankOf = new Map(baseList.map((b, i) => [b.book_id || b, i + 1]));
  const maxDl = baseList.length > 0 ? (baseList[0].download_count || 0) : 0;

  books.forEach((book, idx) => {
    const rank = rankOf.get(book.book_id || book) || (idx + 1);
    const card = buildCard(book, maxDl, rank);
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
  const title   = book.title || book.filename || 'Sin título';
  const author  = book.author || 'Autor desconocido';
  const coverLetter = (book.title || book.filename || '?')[0].toUpperCase();

  // Validar y resolver portada con esquemas seguros + fallback OL por ISBN.
  const isbn       = (book.isbn || '').toString();
  const safeIsbn   = /^[0-9Xx\-]{1,20}$/.test(isbn) ? isbn : '';
  const olFallback = safeIsbn ? `https://covers.openlibrary.org/b/isbn/${encodeURIComponent(safeIsbn)}-M.jpg` : '';
  const primary    = safeUrl(book.cover_url);
  const coverUrl   = primary || olFallback;
  const dlText     = dlCount.toLocaleString('es');

  const article = document.createElement('article');
  article.className = 'vlt-card';
  article.setAttribute('role', 'listitem');
  article.setAttribute('aria-label', title);

  // ── Cover ──────────────────────────────────────────────────────────────────
  const cover = document.createElement('div');
  cover.className = 'vlt-card__cover' + (coverUrl ? ' vlt-card__cover--has-img' : '');
  cover.style.background = gradient;

  if (coverUrl) {
    const img = document.createElement('img');
    img.className = 'vlt-card__cover-img';
    img.src       = coverUrl;
    img.alt       = 'Portada';
    img.loading   = 'eager';
    img.decoding  = 'async';
    // Fallback registrado vía addEventListener (sin handlers inline → CSP-safe).
    img.addEventListener('error', () => {
      if (olFallback && img.src !== olFallback) {
        img.src = olFallback;
      } else {
        img.style.display = 'none';
      }
    }, { once: false });
    cover.appendChild(img);
  }

  const rankSpan = document.createElement('span');
  rankSpan.className = 'vlt-card__rank';
  rankSpan.setAttribute('aria-label', `Posición ${rank}`);
  rankSpan.textContent = `#${rank}`;
  cover.appendChild(rankSpan);

  if (!coverUrl) {
    const letterSpan = document.createElement('span');
    letterSpan.className = 'vlt-card__cover-letter';
    letterSpan.setAttribute('aria-hidden', 'true');
    letterSpan.textContent = coverLetter;
    cover.appendChild(letterSpan);
  }

  const dlBadge = document.createElement('span');
  dlBadge.className = 'vlt-card__dl-badge';
  dlBadge.title     = `${dlText} descargas ${PERIOD_LABELS[state.period] || ''}`;
  dlBadge.innerHTML = `
    <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
      <polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
    </svg>`;
  dlBadge.appendChild(document.createTextNode(' ' + dlText));
  cover.appendChild(dlBadge);

  // ── Body ───────────────────────────────────────────────────────────────────
  const body = document.createElement('div');
  body.className = 'vlt-card__body';

  const h3 = document.createElement('h3');
  h3.className   = 'vlt-card__title';
  h3.textContent = title;

  const pAuthor = document.createElement('p');
  pAuthor.className   = 'vlt-card__author';
  pAuthor.textContent = author;

  const meta = document.createElement('div');
  meta.className = 'vlt-card__meta';
  if (book.year) {
    const y = document.createElement('span');
    y.className = 'vlt-card__year';
    y.textContent = String(book.year);
    meta.appendChild(y);
  }
  if (edition) {
    const e = document.createElement('span');
    e.className = 'vlt-card__edition';
    e.textContent = edition;
    meta.appendChild(e);
  }
  const topicEl = document.createElement('span');
  topicEl.className = 'vlt-card__topic';
  topicEl.textContent = topicLabel;
  meta.appendChild(topicEl);

  body.append(h3, pAuthor, meta);

  // ── Footer ─────────────────────────────────────────────────────────────────
  const footer = document.createElement('div');
  footer.className = 'vlt-card__footer';

  const barWrap  = document.createElement('div');
  barWrap.className = 'vlt-card__bar-wrap';
  const barTrack = document.createElement('div');
  barTrack.className = 'vlt-card__bar-track';
  const barFill  = document.createElement('div');
  barFill.className = 'vlt-card__bar-fill';
  barFill.style.width = `${barPct}%`;
  barTrack.appendChild(barFill);
  const barNum = document.createElement('span');
  barNum.className = 'vlt-card__bar-num';
  barNum.textContent = dlText;
  barWrap.append(barTrack, barNum);

  const dlBtn = document.createElement('button');
  dlBtn.className = 'vlt-card__dl-btn';
  dlBtn.type      = 'button';
  dlBtn.dataset.bookId = String(book.book_id || '');
  dlBtn.dataset.title  = title;
  dlBtn.setAttribute('aria-label', `Descargar ${title}`);
  dlBtn.innerHTML = `
    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" aria-hidden="true">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
      <polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
    </svg> .md`;
  dlBtn.addEventListener('click', e => {
    const btn = e.currentTarget;
    downloadBook(btn.dataset.bookId, btn.dataset.title, btn);
  });

  footer.append(barWrap, dlBtn);

  article.append(cover, body, footer);
  return article;
}

// Whitelist de esquemas — bloquea javascript:, data:, vbscript: en href/src.
function safeUrl(url) {
  if (!url || typeof url !== 'string') return '';
  const trimmed = url.trim();
  if (!trimmed) return '';
  if (/^[/?#]/.test(trimmed)) return trimmed;
  if (/^https?:\/\//i.test(trimmed)) return trimmed;
  return '';
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

  if (state.query) {
    const visible = getFilteredBooks().length;
    label.textContent = `${visible.toLocaleString('es')} de ${state.loaded.length.toLocaleString('es')} cargados · buscando "${state.query}"`;
  } else if (state.total) {
    label.textContent = `${state.total.toLocaleString('es')} libro${state.total !== 1 ? 's' : ''} · más descargados ${pLabel}`;
  } else {
    label.textContent = 'Sin resultados';
  }
  if (totalEl) totalEl.textContent = (state.total || 0).toLocaleString('es');
}

// ─── Pills de filtros activos ─────────────────────────────────────────────────

const EDITION_LABEL = (v) => v === 'na' ? 'Edición N/A' : `${v}ª edición`;

function activeFilterDescriptors() {
  const items = [];
  if (state.period) items.push({ key: 'period', label: `Período: ${PERIOD_LABELS[state.period] || state.period}` });
  if (state.topic)  items.push({ key: 'topic',  label: `Tema: ${TOPIC_LABELS[state.topic] || state.topic}` });
  if (state.year)   items.push({ key: 'year',   label: `Año: ${state.year}` });
  if (state.edition) items.push({ key: 'edition', label: EDITION_LABEL(state.edition) });
  if (state.query)  items.push({ key: 'query',  label: `Búsqueda: "${state.query}"` });
  return items;
}

function updateActiveFilters() {
  const wrap = $('vlt-active');
  const list = $('vlt-active-list');
  if (!wrap || !list) return;

  const items = activeFilterDescriptors();
  list.replaceChildren();

  if (items.length === 0) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;

  items.forEach(({ key, label }) => {
    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'vlt-active__pill';
    pill.setAttribute('aria-label', `Quitar filtro ${label}`);

    const txt = document.createElement('span');
    txt.textContent = label;
    pill.appendChild(txt);

    const x = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    x.setAttribute('width', '11'); x.setAttribute('height', '11');
    x.setAttribute('viewBox', '0 0 24 24'); x.setAttribute('fill', 'none');
    x.setAttribute('stroke', 'currentColor'); x.setAttribute('stroke-width', '2.4');
    x.setAttribute('aria-hidden', 'true');
    const l1 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    l1.setAttribute('x1','18'); l1.setAttribute('y1','6'); l1.setAttribute('x2','6'); l1.setAttribute('y2','18');
    const l2 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    l2.setAttribute('x1','6'); l2.setAttribute('y1','6'); l2.setAttribute('x2','18'); l2.setAttribute('y2','18');
    x.append(l1, l2);
    pill.appendChild(x);

    pill.addEventListener('click', () => removeFilter(key));
    list.appendChild(pill);
  });
}

function removeFilter(key) {
  switch (key) {
    case 'period': {
      state.period = '';
      document.querySelectorAll('.vlt-period-btn').forEach(b => {
        b.classList.remove('vlt-period-btn--active');
        b.setAttribute('aria-pressed', 'false');
      });
      const allBtn = document.querySelector('.vlt-period-btn[data-period=""]');
      if (allBtn) { allBtn.classList.add('vlt-period-btn--active'); allBtn.setAttribute('aria-pressed', 'true'); }
      resetAndFetch();
      break;
    }
    case 'topic':
      state.topic = '';
      document.querySelectorAll('.vlt-chip').forEach(c => c.classList.remove('vlt-chip--active'));
      document.querySelector('.vlt-chip[data-topic=""]')?.classList.add('vlt-chip--active');
      resetAndFetch();
      break;
    case 'year': {
      state.year = '';
      const ys = $('vlt-year-select'); if (ys) ys.value = '';
      resetAndFetch();
      break;
    }
    case 'edition': {
      state.edition = '';
      const es = $('vlt-edition-select'); if (es) es.value = '';
      resetAndFetch();
      break;
    }
    case 'query': {
      const inp = $('vlt-search-input');
      if (inp) inp.value = '';
      setQuery('');
      break;
    }
  }
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
  state.loaded = [];
  fetchBooks(false);
}

function clearAllFilters() {
  state.topic = ''; state.year = ''; state.edition = ''; state.period = '';
  state.query = '';

  document.querySelectorAll('.vlt-period-btn').forEach(b => {
    b.classList.remove('vlt-period-btn--active');
    b.setAttribute('aria-pressed', 'false');
  });
  const allP = document.querySelector('.vlt-period-btn[data-period=""]');
  if (allP) { allP.classList.add('vlt-period-btn--active'); allP.setAttribute('aria-pressed', 'true'); }

  document.querySelectorAll('.vlt-chip').forEach(c => c.classList.remove('vlt-chip--active'));
  document.querySelector('.vlt-chip[data-topic=""]')?.classList.add('vlt-chip--active');

  const ys = $('vlt-year-select'); if (ys) ys.value = '';
  const es = $('vlt-edition-select'); if (es) es.value = '';
  const si = $('vlt-search-input'); if (si) si.value = '';
  const sc = $('vlt-search-clear'); if (sc) sc.hidden = true;

  resetAndFetch();
}

function setQuery(q) {
  const trimmed = (q || '').slice(0, 120);
  state.query = trimmed;
  const clearBtn = $('vlt-search-clear');
  if (clearBtn) clearBtn.hidden = !trimmed;
  // Filtrado puramente cliente-side — no refetch
  renderFiltered();
  // El estado de "cargar más" no cambia: sigue ofreciendo más libros del server.
  updatePagination();
}

function initFilters() {
  // Buscador
  const searchInput = $('vlt-search-input');
  if (searchInput) {
    searchInput.addEventListener('input', e => {
      const val = e.target.value;
      clearTimeout(_searchDebounce);
      _searchDebounce = setTimeout(() => setQuery(val), 180);
    });
    searchInput.addEventListener('keydown', e => {
      if (e.key === 'Escape' && searchInput.value) {
        e.preventDefault();
        searchInput.value = '';
        clearTimeout(_searchDebounce);
        setQuery('');
      }
    });
  }
  $('vlt-search-clear')?.addEventListener('click', () => {
    const inp = $('vlt-search-input');
    if (inp) { inp.value = ''; inp.focus(); }
    clearTimeout(_searchDebounce);
    setQuery('');
  });

  // Atajo global "/" para enfocar el buscador (no roba foco si ya estás escribiendo).
  document.addEventListener('keydown', e => {
    if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    const tag = (t && t.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || (t && t.isContentEditable)) return;
    const inp = $('vlt-search-input');
    if (inp) { e.preventDefault(); inp.focus(); inp.select(); }
  });

  // Limpiar todo (desde la barra de filtros activos)
  $('vlt-active-clear')?.addEventListener('click', () => clearAllFilters());

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

  // Reset btn (estado vacío)
  $('vlt-reset-btn')?.addEventListener('click', () => clearAllFilters());

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
