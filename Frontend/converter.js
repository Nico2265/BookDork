/**
 * converter.js — Convertidor multi-archivo a Markdown
 * Sube hasta 5 archivos en una sola petición, convierte en paralelo
 * en el servidor y permite descargar cada archivo o todos como ZIP.
 */

import { auth, db } from '/static/firebase.js';
import { onAuthStateChanged } from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js';
import {
  doc, getDoc,
} from 'https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js';

// ─── Límites por plan ─────────────────────────────────────────────────────────
const PLAN_LIMITS = {
  gratis: { type: 'lifetime', max: 5,   maxFileBytes: 20  * 1024 * 1024 },
  basic:  { type: 'daily',    max: 50,  maxFileBytes: 50  * 1024 * 1024 },
  pro:    { type: 'daily',    max: 200, maxFileBytes: 200 * 1024 * 1024 },
};

// ─── Estado de suscripción (cargado al iniciar) ────────────────────────────────
let _userData = null;

// ─── Funciones de límite ──────────────────────────────────────────────────────

function today() {
  return new Date().toISOString().split('T')[0];
}

async function loadUserData() {
  const user = auth.currentUser;
  if (!user) return null;
  try {
    const snap = await getDoc(doc(db, 'usuarios', user.uid));
    _userData = snap.exists()
      ? snap.data()
      : { plan: 'gratis', conversiones: 0, conversionesDiarias: 0, fechaConversiones: '' };
  } catch (e) {
    console.warn('Firestore read failed:', e.message);
    _userData = _userData || { plan: 'gratis', conversiones: 0, conversionesDiarias: 0, fechaConversiones: '' };
  }
  return _userData;
}

function getRemainingConversions(data) {
  const plan   = data?.plan || 'gratis';
  const limits = PLAN_LIMITS[plan] || PLAN_LIMITS.gratis;

  if (limits.type === 'lifetime') {
    return Math.max(0, limits.max - (data?.conversiones || 0));
  }

  const d     = data?.fechaConversiones || '';
  const count = d === today() ? (data?.conversionesDiarias || 0) : 0;
  return Math.max(0, limits.max - count);
}

async function checkAndReserveConversions(numFiles) {
  const data      = await loadUserData();
  const remaining = getRemainingConversions(data);

  if (remaining <= 0) {
    showUpgradeModal();
    return false;
  }
  if (numFiles > remaining) {
    showToast(`Solo puedes convertir ${remaining} archivo(s) más con tu plan actual.`, 'error');
    return false;
  }
  return true;
}

// ELIMINADO: incrementConversionCount() — el backend reserva y registra
// los slots de forma atómica en check_and_reserve() antes de convertir.
// Escribir contadores desde el frontend viola el principio de fuente única
// de verdad y permite manipulación directa via Firestore SDK (CRIT-01).

// ─── Banner de plan ───────────────────────────────────────────────────────────

function renderPlanBanner() {
  const banner = document.getElementById('plan-banner');
  if (!banner || !_userData) return;

  const plan      = _userData.plan || 'gratis';
  const remaining = getRemainingConversions(_userData);
  const limits    = PLAN_LIMITS[plan] || PLAN_LIMITS.gratis;

  // Actualizar límite de tamaño de archivo según plan
  MAX_BYTES = limits.maxFileBytes;
  const sizeMB = Math.round(MAX_BYTES / (1024 * 1024));
  const metricEl = document.getElementById('file-size-metric');
  const hintEl   = document.getElementById('dz-hint');
  if (metricEl) metricEl.textContent = `${sizeMB} MB`;
  if (hintEl)   hintEl.textContent   = `Máx. ${sizeMB} MB por archivo · Conversión en paralelo · Descarga individual o ZIP`;

  const tagEl   = document.getElementById('plan-tag');
  const usageEl = document.getElementById('plan-usage');

  const labels = { gratis: 'Gratis', basic: 'Basic', pro: 'Pro' };
  tagEl.textContent = labels[plan] || 'Gratis';
  tagEl.className   = `cv-plan-tag cv-plan-tag--${plan === 'gratis' ? 'free' : plan}`;

  if (limits.type === 'lifetime') {
    const used = limits.max - remaining;
    usageEl.innerHTML = `<strong>${remaining}</strong> de ${limits.max} conversiones restantes (total)`;
  } else {
    usageEl.innerHTML = `<strong>${remaining}</strong> de ${limits.max} conversiones restantes hoy`;
  }

  banner.hidden = false;

  const upgradeBtn = document.getElementById('upgrade-btn');
  if (upgradeBtn) upgradeBtn.hidden = plan === 'pro';
}

// ─── Modal de upgrade ─────────────────────────────────────────────────────────

function showUpgradeModal() {
  const modal = document.getElementById('upgrade-modal');
  if (modal) modal.hidden = false;
}

function hideUpgradeModal() {
  const modal = document.getElementById('upgrade-modal');
  if (modal) modal.hidden = true;
}

// ─── Selección de plan (pago próximamente) ────────────────────────────────────

let _selectedPlan = 'basic';

function startCheckout() {
  showToast(`Plan ${_selectedPlan === 'basic' ? 'Basic ($5/mes)' : 'Pro ($50/mes)'} seleccionado. El sistema de pago estará disponible próximamente.`, 'info');
}

const API_CONVERT   = '/api/convert';
const MAX_FILES     = 5;
let MAX_BYTES       = 50 * 1024 * 1024;   // default Basic; se actualiza tras cargar el plan
const PREVIEW_LIMIT = 6_000;              // chars mostrados en vista previa

const ALLOWED_EXTS = new Set(['.pdf', '.epub', '.mobi', '.azw3', '.djvu', '.txt']);
const FILE_ICONS   = { pdf:'📕', epub:'📗', mobi:'📘', azw3:'📙', djvu:'📓', txt:'📄' };

// ─── Estado ───────────────────────────────────────────────────────────────────

/** @type {File[]} */
let selectedFiles = [];

/** @type {Array<{success:boolean, md_filename?:string, markdown?:string, char_count?:number, word_count?:number, original_filename:string, error?:string}>} */
let conversionResults = [];

// ─── DOM ──────────────────────────────────────────────────────────────────────

const el = {
  dropzone:       document.getElementById('dropzone'),
  fileInput:      document.getElementById('file-input'),
  dzIdle:         document.getElementById('dz-idle'),
  dzHover:        document.getElementById('dz-hover'),
  queueSection:   document.getElementById('queue-section'),
  queueCount:     document.getElementById('queue-count'),
  fileList:       document.getElementById('file-list'),
  addMoreBtn:     document.getElementById('add-more-btn'),
  clearAllBtn:    document.getElementById('clear-all-btn'),
  convertBtn:     document.getElementById('convert-btn'),
  convertLabel:   document.getElementById('convert-label'),
  progressSec:    document.getElementById('progress-section'),
  progressFill:   document.getElementById('progress-fill'),
  progressBar:    document.getElementById('progress-bar'),
  progressLabel:  document.getElementById('progress-label'),
  stepUpload:     document.getElementById('step-upload'),
  stepConvert:    document.getElementById('step-convert'),
  stepDone:       document.getElementById('step-done'),
  resultsSec:     document.getElementById('results-section'),
  resultsTitle:   document.getElementById('results-title'),
  resultsSub:     document.getElementById('results-sub'),
  resultCards:    document.getElementById('result-cards'),
  downloadAllBtn: document.getElementById('download-all-btn'),
  newConvBtn:     document.getElementById('new-conversion-btn'),
  cardTemplate:   document.getElementById('result-card-tpl'),
};

// ─── Helpers ──────────────────────────────────────────────────────────────────

const getExt   = f => { const i = f.lastIndexOf('.'); return i >= 0 ? f.slice(i).toLowerCase() : ''; };
const fmtBytes = b => b < 1024 ? `${b} B` : b < 1048576 ? `${(b/1024).toFixed(1)} KB` : `${(b/1048576).toFixed(1)} MB`;
const fmtNum   = n => n.toLocaleString('es');
const fmtDuration = s => {
  const t = Math.max(0, Math.round(s));
  if (t < 60) return `${t} seg`;
  const m = Math.floor(t / 60), r = t % 60;
  return r > 0 ? `${m} min ${r} seg` : `${m} min`;
};

function _setStep(step, state) {
  if (!step) return;
  step.classList.remove('is-active', 'is-done');
  if (state) step.classList.add(state);
}

function setProgress(pct, label) {
  const v = Math.max(0, Math.min(100, pct));
  el.progressFill.style.width = `${v}%`;
  el.progressBar.setAttribute('aria-valuenow', String(v));
  el.progressLabel.textContent = label;

  if (v < 58) {
    _setStep(el.stepUpload,  'is-active');
    _setStep(el.stepConvert, null);
    _setStep(el.stepDone,    null);
  } else if (v < 100) {
    _setStep(el.stepUpload,  'is-done');
    _setStep(el.stepConvert, 'is-active');
    _setStep(el.stepDone,    null);
  } else {
    _setStep(el.stepUpload,  'is-done');
    _setStep(el.stepConvert, 'is-done');
    _setStep(el.stepDone,    'is-done');
  }
}

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

// ─── Vista (máquina de estados) ───────────────────────────────────────────────

function showState(name) {
  el.dropzone.hidden     = name !== 'idle' && name !== 'queue';
  el.queueSection.hidden = name !== 'queue';
  el.progressSec.hidden  = name !== 'converting';
  el.resultsSec.hidden   = name !== 'done';
  // En estado queue seguimos mostrando la zona de drop (más pequeña vía CSS)
  if (name === 'queue') el.dropzone.classList.add('cv-dropzone--compact');
  else                  el.dropzone.classList.remove('cv-dropzone--compact');
}

// ─── Gestión de archivos ──────────────────────────────────────────────────────

function addFiles(fileList) {
  const remaining = MAX_FILES - selectedFiles.length;
  if (remaining <= 0) {
    showToast(`Límite alcanzado: máximo ${MAX_FILES} archivos.`, 'error');
    return;
  }

  let added = 0, fmtSkipped = 0;
  for (const file of Array.from(fileList).slice(0, remaining)) {
    const ext = getExt(file.name);
    if (!ALLOWED_EXTS.has(ext)) { fmtSkipped++; continue; }
    if (file.size > MAX_BYTES) {
      const planName = (_userData?.plan || 'gratis');
      showToast(`"${file.name}" supera el límite de ${fmtBytes(MAX_BYTES)} del plan ${planName}.`, 'error');
      continue;
    }
    if (selectedFiles.some(f => f.name === file.name && f.size === file.size)) continue; // duplicado
    selectedFiles.push(file);
    added++;
  }

  if (fmtSkipped > 0) showToast(`${fmtSkipped} archivo(s) omitidos: formato no permitido.`, 'error');
  renderFileList();
}

function removeFile(index) {
  selectedFiles.splice(index, 1);
  renderFileList();
}

function clearAll() {
  selectedFiles = [];
  el.fileInput.value = '';
  showState('idle');
}

function renderFileList() {
  if (selectedFiles.length === 0) { showState('idle'); return; }

  el.queueCount.textContent = `${selectedFiles.length} / ${MAX_FILES}`;
  el.convertLabel.textContent = `Convertir ${selectedFiles.length} ${selectedFiles.length === 1 ? 'archivo' : 'archivos'}`;

  el.fileList.innerHTML = '';
  selectedFiles.forEach((file, i) => {
    const ext  = getExt(file.name).slice(1);
    const icon = FILE_ICONS[ext] || '📄';
    const li   = document.createElement('li');
    li.className = 'cv-file-item';

    // Construir con DOM API para evitar XSS via nombre de archivo (HIGH-01)
    const iconSpan = document.createElement('span');
    iconSpan.className   = 'cv-fi-icon';
    iconSpan.textContent = icon;

    const nameSpan = document.createElement('span');
    nameSpan.className   = 'cv-fi-name';
    nameSpan.textContent = file.name;
    nameSpan.title       = file.name;

    const sizeSpan = document.createElement('span');
    sizeSpan.className   = 'cv-fi-size';
    sizeSpan.textContent = fmtBytes(file.size);

    const removeBtn = document.createElement('button');
    removeBtn.className  = 'cv-fi-remove';
    removeBtn.type       = 'button';
    removeBtn.setAttribute('aria-label', `Quitar archivo ${i + 1}`);
    removeBtn.dataset.idx = String(i);
    const svgNS = 'http://www.w3.org/2000/svg';
    const svg   = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('width', '12'); svg.setAttribute('height', '12');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor'); svg.setAttribute('stroke-width', '2');
    const l1 = document.createElementNS(svgNS, 'line');
    l1.setAttribute('x1','18'); l1.setAttribute('y1','6');
    l1.setAttribute('x2','6');  l1.setAttribute('y2','18');
    const l2 = document.createElementNS(svgNS, 'line');
    l2.setAttribute('x1','6');  l2.setAttribute('y1','6');
    l2.setAttribute('x2','18'); l2.setAttribute('y2','18');
    svg.appendChild(l1); svg.appendChild(l2);
    removeBtn.appendChild(svg);

    li.appendChild(iconSpan);
    li.appendChild(nameSpan);
    li.appendChild(sizeSpan);
    li.appendChild(removeBtn);
    el.fileList.appendChild(li);
  });

  el.addMoreBtn.disabled = selectedFiles.length >= MAX_FILES;

  const estimateEl = document.getElementById('queue-estimate');
  if (estimateEl) {
    const totalBytes = selectedFiles.reduce((s, f) => s + f.size, 0);
    const estSecs    = Math.max(15, (totalBytes / 1048576) * 1.5);
    estimateEl.textContent = `${fmtBytes(totalBytes)} total · ~${fmtDuration(estSecs)} estimados`;
  }

  showState('queue');
}

// ─── Upload con progreso real (XHR) ───────────────────────────────────────────

async function uploadFiles(files) {
  const user = auth.currentUser;
  if (!user) throw new Error('Sesión expirada. Vuelve a iniciar sesión.');
  const idToken = await user.getIdToken(false);

  return new Promise((resolve, reject) => {
    const xhr      = new XMLHttpRequest();
    const formData = new FormData();
    files.forEach(f => formData.append('files', f));

    let convInterval = null;
    let _convStart   = 0;

    xhr.upload.addEventListener('progress', e => {
      if (e.lengthComputable)
        setProgress(Math.round((e.loaded / e.total) * 55), 'Subiendo archivos…');
    });

    xhr.upload.addEventListener('load', () => {
      _convStart = Date.now();

      // Estimate conversion time: ~1.5s per MB (pdfminer heuristic, conservative)
      const totalMB    = files.reduce((s, f) => s + f.size, 0) / 1048576;
      const estSeconds = Math.max(15, totalMB * 1.5);

      let pct = 58;
      const TICK_MS    = 300;
      // Distribute 35 pct points evenly over the estimated duration
      const pctPerTick = 35 / (estSeconds * (1000 / TICK_MS));

      const label = () => {
        const elapsed   = (Date.now() - _convStart) / 1000;
        const remaining = Math.max(0, estSeconds - elapsed);
        const suffix    = remaining > 3 ? ` · ~${fmtDuration(remaining)} restantes` : '';
        return `Procesando ${files.length} archivo(s)… ${fmtDuration(elapsed)}${suffix}`;
      };

      setProgress(58, label());
      convInterval = setInterval(() => {
        pct = Math.min(pct + pctPerTick, 93);
        setProgress(pct, label());
        if (pct >= 93) clearInterval(convInterval);
      }, TICK_MS);
    });

    xhr.addEventListener('load', () => {
      if (convInterval) clearInterval(convInterval);
      let data;
      try { data = JSON.parse(xhr.responseText); } catch { data = {}; }

      if (xhr.status === 401) {
        reject(new Error('Sesión expirada. Recarga la página e inicia sesión de nuevo.'));
      } else if (xhr.status === 402) {
        const detail = data.detail || {};
        reject(new Error(
          typeof detail === 'object'
            ? (detail.message || 'Límite de conversiones alcanzado.')
            : String(detail)
        ));
      } else if (xhr.status >= 200 && xhr.status < 300) {
        const elapsed = _convStart ? ` en ${fmtDuration((Date.now() - _convStart) / 1000)}` : '';
        setProgress(100, `Completado${elapsed}`);
        resolve(data);
      } else {
        reject(new Error(
          (typeof data.detail === 'string' ? data.detail : null)
          || `Error del servidor (${xhr.status})`
        ));
      }
    });

    xhr.addEventListener('error', () => {
      if (convInterval) clearInterval(convInterval);
      reject(new Error('Sin conexión con el servidor'));
    });

    xhr.open('POST', API_CONVERT);
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.setRequestHeader('Authorization', `Bearer ${idToken}`);
    xhr.setRequestHeader('ngrok-skip-browser-warning', 'true');
    xhr.send(formData);
  });
}

// ─── Renderizar resultados ────────────────────────────────────────────────────

function renderResults(data) {
  conversionResults = data.results || [];
  const ok    = data.success_count ?? conversionResults.filter(r => r.success).length;
  const total = data.total ?? conversionResults.length;

  el.resultsTitle.textContent = `${ok} de ${total} ${total === 1 ? 'archivo convertido' : 'archivos convertidos'}`;
  el.resultsSub.textContent   = ok < total ? `${total - ok} con error` : 'Todo correcto';

  el.downloadAllBtn.hidden = ok < 2; // ZIP solo si hay 2+ archivos OK
  el.resultCards.innerHTML = '';

  conversionResults.forEach((result, idx) => {
    const card = el.cardTemplate.content.cloneNode(true).querySelector('.cv-result-card');

    const statusIcon = card.querySelector('.cv-rc-status-icon');
    const nameEl     = card.querySelector('.cv-rc-name');
    const statsEl    = card.querySelector('.cv-rc-stats');
    const copyBtn    = card.querySelector('.cv-rc-copy-btn');
    const dlBtn      = card.querySelector('.cv-rc-dl-btn');
    const previewEl  = card.querySelector('.cv-rc-preview');
    const noteEl     = card.querySelector('.cv-rc-preview-note');
    const wrap       = card.querySelector('.cv-rc-preview-wrap');
    const metaPanel  = card.querySelector('.cv-rc-book-meta');

    if (result.success) {
      card.classList.add('cv-result-card--ok');
      statusIcon.textContent = '✓';
      nameEl.textContent     = result.md_filename || result.original_filename;
      statsEl.textContent    = `${fmtNum(result.char_count)} car. · ${fmtNum(result.word_count)} pal.`;

      const preview = result.markdown.length > PREVIEW_LIMIT
        ? result.markdown.slice(0, PREVIEW_LIMIT)
        : result.markdown;
      previewEl.textContent  = preview;
      noteEl.hidden          = result.markdown.length <= PREVIEW_LIMIT;

      copyBtn.addEventListener('click', () => copyText(result.markdown, copyBtn));
      dlBtn.addEventListener('click',   () => downloadFile(result.markdown, result.md_filename));

      // ── Panel de información ISBN (Open Library) ──────────────────────────
      const meta = result.book_meta;
      if (meta && metaPanel) {
        const coverEl    = metaPanel.querySelector('.cv-rc-cover');
        const descEl     = metaPanel.querySelector('.cv-rc-meta-desc');
        const pubEl      = metaPanel.querySelector('.cv-rc-meta-pub');
        const linkEl     = metaPanel.querySelector('.cv-rc-meta-link');
        const subjWrap   = metaPanel.querySelector('.cv-rc-meta-subjects');

        if (meta.cover_url) {
          coverEl.src = meta.cover_url;
          coverEl.alt = meta.title || 'Portada';
        } else {
          coverEl.hidden = true;
        }

        descEl.textContent = meta.description || '';
        descEl.hidden      = !meta.description;

        const pubParts = [meta.publisher, meta.publish_date, meta.pages ? `${meta.pages} pp.` : ''].filter(Boolean);
        pubEl.textContent = pubParts.join(' · ');
        pubEl.hidden      = pubParts.length === 0;

        if (meta.info_url) {
          linkEl.href = meta.info_url;
        } else {
          linkEl.hidden = true;
        }

        (meta.subjects || []).slice(0, 6).forEach(s => {
          const tag = document.createElement('span');
          tag.className   = 'cv-rc-meta-subject';
          tag.textContent = s;
          subjWrap.appendChild(tag);
        });

        metaPanel.hidden = false;
      }
    } else {
      card.classList.add('cv-result-card--err');
      statusIcon.textContent = '✗';
      nameEl.textContent     = result.original_filename;
      statsEl.textContent    = result.error || 'Error desconocido';
      copyBtn.hidden         = true;
      dlBtn.hidden           = true;
      wrap.hidden            = true;
    }

    el.resultCards.appendChild(card);
  });

  showState('done');
}

// ─── Descarga individual ──────────────────────────────────────────────────────

function downloadFile(content, filename) {
  const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
  const url  = URL.createObjectURL(blob);
  const a    = Object.assign(document.createElement('a'), {
    href: url, download: filename, style: 'display:none',
  });
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { document.body.removeChild(a); URL.revokeObjectURL(url); }, 150);
}

// ─── ZIP (implementación nativa sin librerías externas) ───────────────────────

const _crc32Table = (() => {
  const t = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let j = 0; j < 8; j++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    t[i] = c;
  }
  return t;
})();

function _crc32(data) {
  let crc = 0xFFFFFFFF;
  for (let i = 0; i < data.length; i++)
    crc = (crc >>> 8) ^ _crc32Table[(crc ^ data[i]) & 0xFF];
  return (crc ^ 0xFFFFFFFF) >>> 0;
}

function _u16(v, dv, o) { dv.setUint16(o, v, true); }
function _u32(v, dv, o) { dv.setUint32(o, v, true); }

function buildZip(files) {
  // files = [{name: string, content: string}]
  const enc   = new TextEncoder();
  const now   = new Date();
  const dtime = ((now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1));
  const ddate = (((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate());

  const locals  = [];   // Uint8Array[]
  const central = [];   // Uint8Array[]
  const offsets = [];   // byte offset of each local header

  let pos = 0;

  for (const file of files) {
    const nameBytes = enc.encode(file.name);
    const dataBytes = enc.encode(file.content);
    const crc       = _crc32(dataBytes);
    const sz        = dataBytes.length;

    // Local file header (30 + nameLen)
    const lh  = new Uint8Array(30 + nameBytes.length);
    const ldv = new DataView(lh.buffer);
    _u32(0x04034B50, ldv, 0);   // signature
    _u16(20,   ldv,  4);        // version needed
    _u16(0,    ldv,  6);        // flags
    _u16(0,    ldv,  8);        // method: STORE
    _u16(dtime,ldv, 10);
    _u16(ddate,ldv, 12);
    _u32(crc,  ldv, 14);
    _u32(sz,   ldv, 18);        // compressed size
    _u32(sz,   ldv, 22);        // uncompressed size
    _u16(nameBytes.length, ldv, 26);
    _u16(0,    ldv, 28);        // extra length
    lh.set(nameBytes, 30);

    offsets.push(pos);
    locals.push(lh, dataBytes);
    pos += lh.length + dataBytes.length;

    // Central directory entry (46 + nameLen)
    const cd  = new Uint8Array(46 + nameBytes.length);
    const cdv = new DataView(cd.buffer);
    _u32(0x02014B50, cdv, 0);   // signature
    _u16(20,   cdv,  4);        // version made by
    _u16(20,   cdv,  6);        // version needed
    _u16(0,    cdv,  8);        // flags
    _u16(0,    cdv, 10);        // method: STORE
    _u16(dtime,cdv, 12);
    _u16(ddate,cdv, 14);
    _u32(crc,  cdv, 16);
    _u32(sz,   cdv, 20);
    _u32(sz,   cdv, 24);
    _u16(nameBytes.length, cdv, 28);
    _u16(0, cdv, 30); _u16(0, cdv, 32); _u16(0, cdv, 34);
    _u16(0, cdv, 36);
    _u32(0, cdv, 38);
    _u32(offsets[offsets.length - 1], cdv, 42);
    cd.set(nameBytes, 46);
    central.push(cd);
  }

  const cdOffset = pos;
  const cdSize   = central.reduce((s, c) => s + c.length, 0);

  // End of central directory (22 bytes)
  const eocd  = new Uint8Array(22);
  const eodv  = new DataView(eocd.buffer);
  _u32(0x06054B50, eodv,  0);
  _u16(0, eodv, 4); _u16(0, eodv, 6);
  _u16(central.length, eodv,  8);
  _u16(central.length, eodv, 10);
  _u32(cdSize,   eodv, 12);
  _u32(cdOffset, eodv, 16);
  _u16(0, eodv, 20);

  const parts   = [...locals, ...central, eocd];
  const total   = parts.reduce((s, p) => s + p.length, 0);
  const result  = new Uint8Array(total);
  let   offset  = 0;
  for (const p of parts) { result.set(p, offset); offset += p.length; }
  return result;
}

function downloadAll() {
  const okResults = conversionResults.filter(r => r.success);
  if (okResults.length === 0) return;

  const zipData = buildZip(okResults.map(r => ({
    name:    r.md_filename,
    content: r.markdown,
  })));

  const blob = new Blob([zipData], { type: 'application/zip' });
  const url  = URL.createObjectURL(blob);
  const a    = Object.assign(document.createElement('a'), {
    href:     url,
    download: 'bookdork_markdown.zip',
    style:    'display:none',
  });
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { document.body.removeChild(a); URL.revokeObjectURL(url); }, 150);
}

// ─── Copiar al portapapeles ───────────────────────────────────────────────────

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    btn.classList.add('copied');
    const orig = btn.innerHTML;
    btn.textContent = '✓ Copiado';
    setTimeout(() => { btn.classList.remove('copied'); btn.innerHTML = orig; }, 2000);
    showToast('Copiado al portapapeles ✓', 'success');
  } catch {
    showToast('No se pudo copiar — usa Ctrl+A sobre la vista previa', 'error');
  }
}

// ─── Conversión principal ─────────────────────────────────────────────────────

async function startConversion() {
  if (selectedFiles.length === 0) return;

  const allowed = await checkAndReserveConversions(selectedFiles.length);
  if (!allowed) return;

  el.convertBtn.disabled = true;
  showState('converting');
  setProgress(0, 'Preparando…');

  try {
    const data = await uploadFiles(selectedFiles);
    renderResults(data);
    // Recargar datos del plan para reflejar el consumo registrado por el backend
    await loadUserData();
    renderPlanBanner();
  } catch (err) {
    showState('queue');
    el.convertBtn.disabled = false;
    showToast(`Error: ${err.message}`, 'error');
  }
}

// ─── Event listeners ──────────────────────────────────────────────────────────

function init() {
  // Dropzone: clic abre selector
  el.dropzone.addEventListener('click', e => {
    if (e.target !== el.fileInput) el.fileInput.click();
  });
  el.dropzone.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); el.fileInput.click(); }
  });

  // Input file
  el.fileInput.addEventListener('change', () => {
    if (el.fileInput.files.length) addFiles(el.fileInput.files);
    el.fileInput.value = ''; // permite seleccionar el mismo archivo de nuevo
  });

  // Drag & drop
  ['dragenter','dragover','dragleave','drop'].forEach(evt => {
    el.dropzone.addEventListener(evt, e => { e.preventDefault(); e.stopPropagation(); });
    document.body.addEventListener(evt, e => e.preventDefault());
  });
  el.dropzone.addEventListener('dragenter', () => el.dropzone.classList.add('is-dragging'));
  el.dropzone.addEventListener('dragover',  () => el.dropzone.classList.add('is-dragging'));
  el.dropzone.addEventListener('dragleave', e => {
    if (!el.dropzone.contains(e.relatedTarget)) el.dropzone.classList.remove('is-dragging');
  });
  el.dropzone.addEventListener('drop', e => {
    el.dropzone.classList.remove('is-dragging');
    if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files);
  });

  // Botones de la cola
  el.addMoreBtn.addEventListener('click', () => el.fileInput.click());
  el.clearAllBtn.addEventListener('click', clearAll);

  // Quitar archivo individual (delegación)
  el.fileList.addEventListener('click', e => {
    const btn = e.target.closest('.cv-fi-remove');
    if (btn) removeFile(Number(btn.dataset.idx));
  });

  el.convertBtn.addEventListener('click', startConversion);

  // Resultados
  el.downloadAllBtn.addEventListener('click', downloadAll);
  el.newConvBtn.addEventListener('click', () => {
    selectedFiles = [];
    conversionResults = [];
    el.fileInput.value = '';
    el.convertBtn.disabled = false;
    showState('idle');
  });
}

document.addEventListener('DOMContentLoaded', () => {
  init();

  // Modal upgrade
  document.getElementById('upgrade-btn')?.addEventListener('click', showUpgradeModal);
  document.getElementById('upgrade-modal-close')?.addEventListener('click', hideUpgradeModal);
  document.getElementById('upgrade-modal')?.addEventListener('click', e => {
    if (e.target === e.currentTarget) hideUpgradeModal();
  });

  // Selección de plan en el modal
  function selectPlan(plan) {
    _selectedPlan = plan;
    const labels = { basic: 'Basic — $5/mes', pro: 'Pro — $50/mes' };
    const cta = document.getElementById('upgrade-cta-btn');
    if (cta) cta.textContent = `Suscribirse a ${labels[plan]}`;
    document.getElementById('opt-basic')?.classList.toggle('cv-plan-option--selected', plan === 'basic');
    document.getElementById('opt-pro')?.classList.toggle('cv-plan-option--selected', plan === 'pro');
  }

  document.getElementById('opt-basic')?.addEventListener('click', () => selectPlan('basic'));
  document.getElementById('opt-pro')?.addEventListener('click',  () => selectPlan('pro'));

  document.getElementById('upgrade-cta-btn')?.addEventListener('click', startCheckout);

  // Cargar datos del usuario cuando esté autenticado
  onAuthStateChanged(auth, async user => {
    if (user) {
      await loadUserData();
      renderPlanBanner();
    }
  });

  // Mostrar mensaje de bienvenida tras suscripción exitosa
  if (new URLSearchParams(window.location.search).get('subscribed') === '1') {
    history.replaceState({}, '', '/converter');
    setTimeout(() => showToast('¡Suscripción activada! Tu plan ya está disponible.', 'success'), 800);
  }
});
