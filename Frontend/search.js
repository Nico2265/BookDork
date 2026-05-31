/**
 * search.js — Lógica de búsqueda y comunicación con la API
 */

import { ui } from '/static/ui.js';

const API_BASE = window.location.origin + '/api';
const DEBOUNCE_MS = 400;
const MAX_QUERY_LENGTH = 512;
const OL_COVER_BASE = 'https://covers.openlibrary.org/b/id/';

let currentAbortController = null;
let debounceTimer = null;

const state = {
  query:    '',
  filetype: 'any',
  site:     'any',
  language: 'any',
  author:   '',
  isbn:     '',
  page:     1,
  limit:    20,
  totalHits: 0,
  isLoading: false,
};

const $ = (id) => document.getElementById(id);

const dom = {
  form:             $('search-form'),
  queryInput:       $('search-query'),
  clearBtn:         $('clear-btn'),
  spinner:          $('search-spinner'),
  resultsSection:   $('results-section'),
  resultsGrid:      $('results-grid'),
  resultsCount:     $('results-count'),
  resultsTime:      $('results-time'),
  resultsSourceTag: $('results-source-tag'),
  emptyState:       $('empty-state'),
  emptyDorkBtn:     $('empty-dork-btn'),
  suggestionsBar:   $('suggestions-bar'),
  suggestionsList:  $('suggestions-list'),
  pagination:       $('pagination'),
  prevPage:         $('prev-page'),
  nextPage:         $('next-page'),
  pageIndicator:    $('page-indicator'),
  welcomeSection:   $('welcome-section'),
  filtersPanel:     $('filters-panel'),
  filtersBadge:     $('filters-badge'),
  filterFiletype:   $('filter-filetype'),
  filterSite:       $('filter-site'),
  filterLanguage:   $('filter-language'),
  filterAuthor:     $('filter-author'),
  filterIsbn:       $('filter-isbn'),
  activeFilters:    $('active-filters'),
  activeFiltersList: $('active-filters-list'),
  activeFiltersClear: $('active-filters-clear'),
};

// Etiquetas legibles para las pills de filtros activos
const FILETYPE_LABELS = { pdf: 'PDF', epub: 'EPUB', mobi: 'MOBI', djvu: 'DjVu', azw3: 'AZW3', txt: 'TXT' };
const LANGUAGE_LABELS = { es: 'Español', en: 'English', fr: 'Français', de: 'Deutsch', pt: 'Português' };
const SITE_LABELS = {
  'archive.org': 'Internet Archive',
  'gutenberg.org': 'Project Gutenberg',
  'openlibrary.org': 'Open Library',
  'pdfdrive.com': 'PDF Drive',
  'books.google.com': 'Google Books',
};

// ─── API calls ────────────────────────────────────────────────────────────────

async function fetchSearch(params) {
  if (currentAbortController) currentAbortController.abort();
  currentAbortController = new AbortController();
  const { signal } = currentAbortController;

  const url = new URL(`${API_BASE}/search`);
  Object.entries(params).forEach(([key, val]) => {
    if (val !== null && val !== undefined && val !== '' && val !== 'any') {
      url.searchParams.set(key, String(val));
    }
  });
  url.searchParams.set('q', params.q);

  try {
    const response = await fetch(url.toString(), {
      method: 'GET',
      signal,
      headers: { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'ngrok-skip-browser-warning': 'true' },
      credentials: 'omit',
    });
    if (!response.ok) {
      const errData = await response.json().catch(() => ({}));
      throw new Error(errData.detail || `Error ${response.status}`);
    }
    return await response.json();
  } catch (err) {
    if (err.name === 'AbortError') return null;
    throw err;
  }
}

async function fetchOpenLibrary(query, filetype, author) {
  try {
    // Usamos el proxy del backend para evitar problemas de CORS/red desde el navegador
    const url = new URL(`${API_BASE}/search/external`);
    url.searchParams.set('q', author ? `${query} ${author}` : query);
    url.searchParams.set('limit', '15');

    const res = await fetch(url.toString(), {
      credentials: 'omit',
      headers: { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest', 'ngrok-skip-browser-warning': 'true' },
    });
    if (!res.ok) return [];
    const data = await res.json();

    const ft = filetype && filetype !== 'any' ? filetype : 'pdf';
    return (data.docs || []).map(doc => ({
      id:          doc.key || '',
      title:       doc.title || 'Sin título',
      author:      doc.author_name ? doc.author_name[0] : null,
      description: null,
      filetype:    ft,
      language:    doc.language ? doc.language[0] : null,
      year:        doc.first_publish_year || null,
      source_site: 'openlibrary.org',
      isbn:        doc.isbn ? doc.isbn[0] : null,
      cover_url:   doc.cover_i ? `${OL_COVER_BASE}${doc.cover_i}-M.jpg` : null,
      dork_url:    buildDorkUrl(doc.title, doc.author_name?.[0], ft),
      tags:        doc.subject ? doc.subject.slice(0, 5) : [],
    }));
  } catch {
    return [];
  }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function buildDorkUrl(title, author, filetype) {
  const ft = filetype && filetype !== 'any' ? filetype : 'pdf';
  const safeTitle = (title || '').replace(/"/g, '');
  const parts = [`"${safeTitle}"`, `filetype:${ft}`];
  if (author) {
    // Solo apellido sin comillas: más tolerante a variaciones de nombre en el PDF
    const lastName = author.replace(/"/g, '').trim().split(/\s+/).at(-1) || '';
    if (lastName.length > 2) parts.push(lastName);
  }
  return `https://www.google.com/search?q=${encodeURIComponent(parts.join(' '))}`;
}

function buildAltDorkUrl(title, author, filetype) {
  const ft = filetype && filetype !== 'any' ? filetype : 'pdf';
  const safeTitle = (title || '').replace(/"/g, '');
  // Sin filetype:, busca páginas que mencionan el título + "pdf" como palabra clave
  const parts = [`"${safeTitle}"`, ft];
  if (author) {
    const lastName = author.replace(/"/g, '').trim().split(/\s+/).at(-1) || '';
    if (lastName.length > 2) parts.push(lastName);
  }
  return `https://www.google.com/search?q=${encodeURIComponent(parts.join(' '))}`;
}

// Whitelist de esquemas seguros para href/src — bloquea javascript:, data:, vbscript:
// y otros esquemas activos que podrían ejecutarse en el contexto del documento.
function safeUrl(url) {
  if (!url || typeof url !== 'string') return '';
  const trimmed = url.trim();
  if (!trimmed) return '';
  // URLs relativas / fragmentos / queries son seguras.
  if (/^[/?#]/.test(trimmed)) return trimmed;
  // Solo http(s) absolutas. mailto: y tel: no aplican a este contexto.
  if (/^https?:\/\//i.test(trimmed)) return trimmed;
  return '';
}

function sanitizeHighlight(html) {
  if (!html) return '';
  const escaped = html
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  return escaped
    .replace(/&lt;mark&gt;/g, '<mark>')
    .replace(/&lt;\/mark&gt;/g, '</mark>');
}

const FILETYPE_COLORS = {
  pdf: 'pdf', epub: 'epub', mobi: 'mobi', djvu: 'djvu', azw3: 'azw3', txt: 'txt',
};

// ─── Card creation ────────────────────────────────────────────────────────────

function createBookCard(book) {
  const template = document.getElementById('book-card-template');
  const card = template.content.cloneNode(true).querySelector('.book-card');

  // Cover
  const coverWrap = card.querySelector('.book-cover-wrap');
  const coverImg  = card.querySelector('.book-cover');
  const safeCover = safeUrl(book.cover_url);
  if (safeCover && coverImg) {
    coverImg.src = safeCover;
    coverImg.alt = `Portada de ${book.title}`;
    coverImg.addEventListener('load',  () => card.classList.add('has-cover'), { once: true });
    coverImg.addEventListener('error', () => { if (coverWrap) coverWrap.hidden = true; }, { once: true });
  } else {
    if (coverWrap) coverWrap.hidden = true;
  }

  // Filetype badge
  const filetypeBadge = card.querySelector('.book-filetype-badge');
  if (filetypeBadge) {
    if (book.filetype) {
      filetypeBadge.textContent = book.filetype.toUpperCase();
      filetypeBadge.dataset.type = FILETYPE_COLORS[book.filetype] || 'default';
    } else {
      filetypeBadge.hidden = true;
    }
  }

  // Source badge
  const sourceBadge = card.querySelector('.book-source-badge');
  if (sourceBadge) {
    if (book.source_site) {
      sourceBadge.textContent = book.source_site.replace(/^www\./, '');
    } else {
      sourceBadge.hidden = true;
    }
  }

  // Language badge
  const langBadge = card.querySelector('.book-lang-badge');
  if (langBadge) {
    if (book.language && book.language !== 'any') {
      langBadge.textContent = book.language.toUpperCase();
    } else {
      langBadge.hidden = true;
    }
  }

  // Year
  const yearEl = card.querySelector('.book-year');
  if (yearEl) yearEl.textContent = book.year ? String(book.year) : '';

  // Title
  const titleEl = card.querySelector('.book-title');
  if (titleEl) titleEl.innerHTML = sanitizeHighlight(book.title || 'Sin título');

  // Author
  const authorEl = card.querySelector('.book-author');
  if (authorEl) {
    if (book.author) {
      authorEl.textContent = `por ${book.author}`;
    } else {
      authorEl.hidden = true;
    }
  }

  // Description
  const descEl = card.querySelector('.book-description');
  if (descEl) {
    if (book.description) {
      descEl.innerHTML = sanitizeHighlight(book.description);
    } else {
      descEl.hidden = true;
    }
  }

  // Tags
  const tagsEl = card.querySelector('.book-tags');
  if (tagsEl) {
    if (book.tags && book.tags.length > 0) {
      book.tags.slice(0, 5).forEach(tag => {
        const span = document.createElement('span');
        span.className = 'book-tag';
        span.textContent = tag;
        tagsEl.appendChild(span);
      });
    } else {
      tagsEl.hidden = true;
    }
  }

  // ISBN
  const isbnEl = card.querySelector('.book-isbn');
  if (isbnEl) isbnEl.textContent = book.isbn ? `ISBN: ${book.isbn}` : '';

  // Botón principal: filetype:pdf exacto
  const findBtn = card.querySelector('.btn-find');
  if (findBtn) {
    findBtn.href = safeUrl(book.dork_url) || '#';
    const ftLabel = book.filetype ? book.filetype.toUpperCase() : 'PDF';
    const textNode = Array.from(findBtn.childNodes).find(n => n.nodeType === Node.TEXT_NODE);
    if (textNode) textNode.textContent = `Buscar ${ftLabel} `;
    findBtn.setAttribute('aria-label', `Buscar "${book.title}" en Google como ${ftLabel} (se abre en una pestaña nueva)`);
  }

  // Botón alternativo: sin filetype:, busca páginas de descarga
  const altBtn = card.querySelector('.btn-find-alt');
  if (altBtn) {
    altBtn.href = safeUrl(buildAltDorkUrl(book.title, book.author, book.filetype)) || '#';
    altBtn.setAttribute('aria-label', `Búsqueda amplia de "${book.title}" en la web (se abre en una pestaña nueva)`);
  }

  return card;
}

// ─── Skeletons ────────────────────────────────────────────────────────────────

function renderSkeletons(count = 6) {
  dom.resultsGrid.replaceChildren();
  const frag = document.createDocumentFragment();
  for (let i = 0; i < count; i++) {
    const skel = document.createElement('article');
    skel.className = 'book-card book-card--skeleton';
    skel.setAttribute('aria-hidden', 'true');

    const cover = document.createElement('div');
    cover.className = 'skel-cover';

    const body = document.createElement('div');
    body.className = 'skel-body';

    const row = document.createElement('div');
    row.className = 'skel-row';
    for (let b = 0; b < 3; b++) {
      const badge = document.createElement('span');
      badge.className = 'skel-badge';
      row.appendChild(badge);
    }

    const title = document.createElement('div');
    title.className = 'skel-line skel-title';
    const author = document.createElement('div');
    author.className = 'skel-line skel-author';
    const desc1 = document.createElement('div');
    desc1.className = 'skel-line skel-desc';
    const desc2 = document.createElement('div');
    desc2.className = 'skel-line skel-desc-2';
    const footer = document.createElement('div');
    footer.className = 'skel-line skel-footer';

    body.append(row, title, author, desc1, desc2, footer);
    skel.append(cover, body);
    frag.appendChild(skel);
  }
  dom.resultsGrid.appendChild(frag);
}

// ─── Render ───────────────────────────────────────────────────────────────────

function renderBookList(books) {
  dom.resultsGrid.innerHTML = '';
  const fragment = document.createDocumentFragment();
  books.forEach(book => fragment.appendChild(createBookCard(book)));
  dom.resultsGrid.appendChild(fragment);
}

function renderResults(data, fromOpenLibrary = false) {
  dom.welcomeSection.hidden = true;
  dom.resultsSection.hidden = false;

  state.totalHits = data.total_hits;

  dom.resultsCount.textContent =
    `${data.total_hits.toLocaleString('es')} resultado${data.total_hits !== 1 ? 's' : ''}`;
  dom.resultsTime.textContent = data.processing_time_ms ? `(${data.processing_time_ms} ms)` : '';

  if (dom.resultsSourceTag) {
    if (fromOpenLibrary) {
      dom.resultsSourceTag.textContent = 'Open Library';
      dom.resultsSourceTag.hidden = false;
    } else {
      dom.resultsSourceTag.hidden = true;
    }
  }

  // Suggestions
  if (data.suggestions && data.suggestions.length > 0) {
    dom.suggestionsBar.hidden = false;
    dom.suggestionsList.innerHTML = '';
    data.suggestions.forEach(sug => {
      const btn = document.createElement('button');
      btn.className = 'suggestion-btn';
      btn.textContent = sug;
      btn.addEventListener('click', () => {
        dom.queryInput.value = sug;
        state.query = sug;
        state.page = 1;
        triggerSearch();
      });
      dom.suggestionsList.appendChild(btn);
    });
  } else {
    dom.suggestionsBar.hidden = true;
  }

  if (!data.results || data.results.length === 0) {
    dom.emptyState.hidden = false;
    if (dom.emptyDorkBtn) dom.emptyDorkBtn.href = safeUrl(data.dork_url) || '#';
    dom.pagination.hidden = true;
  } else {
    dom.emptyState.hidden = true;
    renderBookList(data.results);
    renderPagination(data.page, data.limit, data.total_hits);
  }
}

function renderPagination(page, limit, total) {
  const totalPages = Math.ceil(total / limit);
  if (totalPages <= 1) {
    dom.pagination.hidden = true;
    return;
  }
  dom.pagination.hidden = false;
  dom.pageIndicator.textContent = `Página ${page} de ${totalPages}`;
  dom.prevPage.disabled = page <= 1;
  dom.nextPage.disabled = page >= totalPages;
}

// ─── Search flow ──────────────────────────────────────────────────────────────

async function triggerSearch() {
  const query = state.query.trim();
  if (query.length < 2) return;
  if (query.length > MAX_QUERY_LENGTH) {
    ui.showToast('La consulta es demasiado larga (máx. 512 caracteres)', 'error');
    return;
  }

  state.isLoading = true;
  setLoadingState(true);
  renderSkeletons(6);
  dom.resultsSection.hidden = false;
  dom.welcomeSection.hidden = true;

  try {
    const params = {
      q:        query,
      filetype: state.filetype,
      site:     state.site,
      language: state.language,
      author:   state.author || undefined,
      isbn:     state.isbn || undefined,
      page:     state.page,
      limit:    state.limit,
    };

    let backendData = null;
    let suggestions = [];
    let dorkUrl = buildDorkUrl(query, state.author || null, state.filetype);

    try {
      backendData = await fetchSearch(params);
      if (backendData === null) return; // cancelled by a newer search
      suggestions = backendData.suggestions || [];
      dorkUrl = backendData.dork_url || dorkUrl;
    } catch {
      // Server unreachable — will try Open Library below
    }

    // Backend returned results → show them directly
    if (backendData && backendData.results && backendData.results.length > 0) {
      renderResults(backendData);
      return;
    }

    // Backend has no results or is down → try Open Library
    const olBooks = await fetchOpenLibrary(query, state.filetype, state.author || null);
    if (olBooks.length > 0) {
      renderResults({
        total_hits:         olBooks.length,
        page:               1,
        limit:              olBooks.length,
        processing_time_ms: null,
        results:            olBooks,
        suggestions,
        dork_url:           dorkUrl,
      }, true);
      return;
    }

    // Nothing anywhere → show empty state
    renderResults({
      total_hits:         0,
      page:               state.page,
      limit:              state.limit,
      processing_time_ms: backendData ? backendData.processing_time_ms : null,
      results:            [],
      suggestions,
      dork_url:           dorkUrl,
    });

  } catch (err) {
    dom.resultsGrid.innerHTML = '';
    dom.emptyState.hidden = false;
    const h2 = dom.emptyState.querySelector('h2');
    const p  = dom.emptyState.querySelector('p');
    if (h2) h2.textContent = 'Error inesperado';
    if (p)  p.textContent  = err.message;
    ui.showToast(`Error: ${err.message}`, 'error');
  } finally {
    state.isLoading = false;
    setLoadingState(false);
  }
}

function setLoadingState(loading) {
  dom.spinner.hidden = !loading;
  const submitBtn = dom.form.querySelector('.search-btn');
  const btnText = submitBtn.querySelector('.search-btn__text') || submitBtn;
  submitBtn.disabled = loading;
  submitBtn.dataset.loading = loading ? 'true' : 'false';
  submitBtn.setAttribute('aria-busy', loading ? 'true' : 'false');
  btnText.textContent = loading ? 'Buscando…' : 'Buscar';
  dom.resultsSection.setAttribute('aria-busy', loading ? 'true' : 'false');
}

// ─── Utilities ────────────────────────────────────────────────────────────────

function countActiveFilters() {
  return [
    state.filetype !== 'any',
    state.site !== 'any',
    state.language !== 'any',
    Boolean(state.author),
    Boolean(state.isbn),
  ].filter(Boolean).length;
}

function updateFiltersBadge() {
  const count = countActiveFilters();
  dom.filtersBadge.hidden = count === 0;
  dom.filtersBadge.textContent = String(count);
  renderActiveFilterPills();
}

// Descriptores de filtros activos para las pills visibles
function activeFilterDescriptors() {
  const items = [];
  if (state.filetype !== 'any') items.push({ key: 'filetype', label: `Formato: ${FILETYPE_LABELS[state.filetype] || state.filetype.toUpperCase()}` });
  if (state.language !== 'any') items.push({ key: 'language', label: `Idioma: ${LANGUAGE_LABELS[state.language] || state.language}` });
  if (state.site !== 'any')     items.push({ key: 'site',     label: `Fuente: ${SITE_LABELS[state.site] || state.site}` });
  if (state.author)             items.push({ key: 'author',   label: `Autor: ${state.author}` });
  if (state.isbn)               items.push({ key: 'isbn',     label: `ISBN: ${state.isbn}` });
  return items;
}

function renderActiveFilterPills() {
  if (!dom.activeFilters || !dom.activeFiltersList) return;
  const items = activeFilterDescriptors();
  dom.activeFiltersList.replaceChildren();

  if (items.length === 0) {
    dom.activeFilters.hidden = true;
    return;
  }
  dom.activeFilters.hidden = false;

  items.forEach(({ key, label }) => {
    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'active-filter-pill';
    pill.setAttribute('aria-label', `Quitar filtro ${label}`);

    const txt = document.createElement('span');
    txt.textContent = label;

    const x = document.createElement('span');
    x.className = 'active-filter-pill__x';
    x.setAttribute('aria-hidden', 'true');
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '10'); svg.setAttribute('height', '10');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor'); svg.setAttribute('stroke-width', '2.4');
    const l1 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    l1.setAttribute('x1','18'); l1.setAttribute('y1','6'); l1.setAttribute('x2','6'); l1.setAttribute('y2','18');
    const l2 = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    l2.setAttribute('x1','6'); l2.setAttribute('y1','6'); l2.setAttribute('x2','18'); l2.setAttribute('y2','18');
    svg.append(l1, l2);
    x.appendChild(svg);

    pill.append(txt, x);
    pill.addEventListener('click', () => removeActiveFilter(key));
    dom.activeFiltersList.appendChild(pill);
  });
}

function removeActiveFilter(key) {
  switch (key) {
    case 'filetype': state.filetype = 'any'; dom.filterFiletype.value = 'any'; break;
    case 'language': state.language = 'any'; dom.filterLanguage.value = 'any'; break;
    case 'site':     state.site     = 'any'; dom.filterSite.value     = 'any'; break;
    case 'author':   state.author   = '';    dom.filterAuthor.value   = '';    break;
    case 'isbn':     state.isbn     = '';    dom.filterIsbn.value     = '';    break;
    default: return;
  }
  state.page = 1;
  updateFiltersBadge();
  if (state.query.length >= 2) triggerSearch();
}

function clearAllActiveFilters() {
  state.filetype = 'any'; dom.filterFiletype.value = 'any';
  state.language = 'any'; dom.filterLanguage.value = 'any';
  state.site     = 'any'; dom.filterSite.value     = 'any';
  state.author   = '';    dom.filterAuthor.value   = '';
  state.isbn     = '';    dom.filterIsbn.value     = '';
  state.page = 1;
  updateFiltersBadge();
  if (state.query.length >= 2) triggerSearch();
}

// ─── Event listeners ──────────────────────────────────────────────────────────

function initSearchListeners() {
  dom.form.addEventListener('submit', (e) => {
    e.preventDefault();
    state.query = dom.queryInput.value.trim();
    state.page = 1;
    if (state.query.length >= 2) triggerSearch();
  });

  dom.queryInput.addEventListener('input', () => {
    const val = dom.queryInput.value.trim();
    dom.clearBtn.hidden = val.length === 0;
    state.query = val;
    state.page = 1;
    clearTimeout(debounceTimer);
    if (val.length >= 2) {
      debounceTimer = setTimeout(() => triggerSearch(), DEBOUNCE_MS);
    } else if (val.length === 0) {
      dom.resultsSection.hidden = true;
      dom.welcomeSection.hidden = false;
    }
  });

  dom.clearBtn.addEventListener('click', () => {
    dom.queryInput.value = '';
    dom.clearBtn.hidden = true;
    state.query = '';
    state.page = 1;
    dom.resultsSection.hidden = true;
    dom.welcomeSection.hidden = false;
    dom.queryInput.focus();
  });

  const filterChangeHandler = () => {
    state.filetype = dom.filterFiletype.value;
    state.site     = dom.filterSite.value;
    state.language = dom.filterLanguage.value;
    state.author   = dom.filterAuthor.value.trim();
    state.isbn     = dom.filterIsbn.value.trim();
    state.page     = 1;
    updateFiltersBadge();
    if (state.query.length >= 2) {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => triggerSearch(), 200);
    }
  };

  [dom.filterFiletype, dom.filterSite, dom.filterLanguage].forEach(el =>
    el.addEventListener('change', filterChangeHandler)
  );
  [dom.filterAuthor, dom.filterIsbn].forEach(el =>
    el.addEventListener('input', filterChangeHandler)
  );

  // Pills de filtros activos: botón "Limpiar todo"
  dom.activeFiltersClear?.addEventListener('click', clearAllActiveFilters);

  dom.prevPage.addEventListener('click', () => {
    if (state.page > 1) {
      state.page--;
      triggerSearch();
      dom.resultsSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  });

  dom.nextPage.addEventListener('click', () => {
    const totalPages = Math.ceil(state.totalHits / state.limit);
    if (state.page < totalPages) {
      state.page++;
      triggerSearch();
      dom.resultsSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && document.activeElement === dom.queryInput) dom.clearBtn.click();
  });

  document.addEventListener('keydown', (e) => {
    if (
      e.key === '/' &&
      document.activeElement !== dom.queryInput &&
      document.activeElement.tagName !== 'INPUT' &&
      document.activeElement.tagName !== 'TEXTAREA'
    ) {
      e.preventDefault();
      dom.queryInput.focus();
    }
  });
}

// ─── Init ─────────────────────────────────────────────────────────────────────

function loadFromURL() {
  const params = new URLSearchParams(window.location.search);
  const q = params.get('q');
  if (q) {
    dom.queryInput.value = q;
    dom.clearBtn.hidden = false;
    state.query = q;
    const filetype = params.get('filetype');
    if (filetype) { dom.filterFiletype.value = filetype; state.filetype = filetype; }
    const site = params.get('site');
    if (site) { dom.filterSite.value = site; state.site = site; }
    triggerSearch();
  }
}

document.addEventListener('DOMContentLoaded', () => {
  initSearchListeners();
  loadFromURL();
  // Exponer API de depuración solo en desarrollo local
  if (location.hostname === 'localhost' || location.hostname === '127.0.0.1') {
    window._bookdorkSearch = { trigger: triggerSearch, state };
  }
});

export { triggerSearch, state };
