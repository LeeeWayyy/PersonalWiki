import { headersFor, streamJob } from './backend-client.js';
import { t } from '../lib/i18n.mjs';

// t() is safe at module load: the language is fixed per page load (toggle reloads).
const STATUS = {
  online: [t('be.online'), 'var(--sage)'],
  error: [t('be.error'), 'var(--text-3)'],
  offline: [t('be.offline'), 'var(--text-3)'],
};

function el(id) {
  return document.getElementById(id);
}

function appendTo(log, text, showLog) {
  if (showLog) log.style.display = 'block';
  log.textContent += (log.textContent ? '\n' : '') + text;
  log.scrollTop = log.scrollHeight;
}

export function ingestOptions(kind, sectionHeading) {
  const supportsSection = kind === 'auto' || kind === 'wiki';
  return {
    kind,
    section_heading: supportsSection ? sectionHeading.trim() || null : null,
  };
}

export const isSectionableFile = (name) => /\.(epub|mobi|azw3?|pdf|md|markdown|txt|html?)$/i.test(name);

export function mount(config) {
  const urlInput = el(config.urlId);
  const tokenInput = el(config.tokenId);
  const status = el(config.statusId);
  const log = el(config.logId);
  const cancelBtn = el(config.cancelId);
  const kindInput = el(config.kindId);
  const sectionHeadingInput = el(config.sectionHeadingId);
  const sectionList = config.sectionListId ? el(config.sectionListId) : null;
  const sectionsLoadBtn = config.sectionsLoadId ? el(config.sectionsLoadId) : null;
  const fileNameEl = config.fileNameId ? el(config.fileNameId) : null;
  let activeJob = null;
  let activeHeaders = {};
  let mode = 'url';
  let backendOnline = false;

  function syncSectionSupport() {
    // The pipeline rejects --section for lang (its generator works through
    // chapters itself) and video; explain the gray-out instead of just graying.
    const unsupported = kindInput.value !== 'auto' && kindInput.value !== 'wiki';
    const why = unsupported ? t('ingest.sectionNA') : '';
    sectionHeadingInput.disabled = unsupported;
    sectionHeadingInput.title = why;
    if (sectionsLoadBtn) {
      sectionsLoadBtn.disabled = unsupported;
      sectionsLoadBtn.title = why;
    }
  }

  kindInput.addEventListener('change', syncSectionSupport);
  syncSectionSupport();

  function baseUrl() {
    return localStorage.getItem('backendUrl') || urlInput.value;
  }

  function authHeaders() {
    return headersFor(localStorage.getItem('backendToken') || tokenInput.value);
  }

  function append(text) {
    appendTo(log, text, true);
  }

  async function ping() {
    try {
      const r = await fetch(urlInput.value + '/health');
      backendOnline = r.ok;
      const [text, color] = r.ok ? STATUS.online : STATUS.error;
      status.textContent = text;
      status.style.color = color;
    } catch {
      backendOnline = false;
      const [text, color] = STATUS.offline;
      status.textContent = text;
      status.style.color = color;
    }
  }

  urlInput.value = localStorage.getItem('backendUrl') || 'http://localhost:8787';
  tokenInput.value = localStorage.getItem('backendToken') || '';
  el(config.saveId).onclick = () => {
    localStorage.setItem('backendUrl', urlInput.value);
    localStorage.setItem('backendToken', tokenInput.value);
    if (config.settingsId) el(config.settingsId).style.display = 'none';
    ping();
  };
  if (config.toggleId && config.settingsId) {
    el(config.toggleId).onclick = () => {
      const settings = el(config.settingsId);
      settings.style.display = settings.style.display === 'none' ? 'flex' : 'none';
    };
  }

  // Build fetch args for a file-or-url POST; returns {error} when input is missing.
  function sourcePayload(H, opts) {
    if (mode === 'file') {
      const file = el(config.fileInputId).files[0];
      if (!file) return { error: t('ingest.err.file') };
      const fd = new FormData();
      fd.append('file', file);
      if (opts) fd.append('options', JSON.stringify(opts));
      return { headers: H, body: fd };
    }
    const target = el(config.urlInputId).value.trim();
    if (!target) return { error: t('ingest.err.url') };
    return {
      headers: { ...H, 'Content-Type': 'application/json' },
      body: JSON.stringify(opts ? { url: target, options: opts } : { url: target }),
    };
  }

  if (config.fileInputId) {
    el(config.fileInputId).addEventListener('change', (e) => {
      const file = e.target.files[0];
      if (!file) return;
      if (fileNameEl) fileNameEl.textContent = file.name;
      if (config.onSectionableFile && isSectionableFile(file.name)) config.onSectionableFile();
    });
  }

  if (sectionsLoadBtn && sectionList) {
    sectionsLoadBtn.onclick = async () => {
      const payload = sourcePayload(authHeaders());
      if (payload.error) return append(payload.error);
      sectionsLoadBtn.disabled = true;
      try {
        const res = await fetch(baseUrl() + '/ingest/sections', { method: 'POST', ...payload });
        if (!res.ok) throw new Error(await res.text());
        const { sections } = await res.json();
        sectionList.replaceChildren(
          ...sections.map((s) => Object.assign(document.createElement('option'), { value: s })),
        );
        append(t('ingest.sectionsLoaded', { n: sections.length }));
        sectionHeadingInput.focus();
      } catch (e) {
        append(t('ingest.sectionsErr') + e.message);
      } finally {
        syncSectionSupport();
      }
    };
  }

  if (config.segUrlId && config.segFileId) {
    const segUrl = el(config.segUrlId);
    const segFile = el(config.segFileId);
    function setMode(next) {
      mode = next;
      segUrl.classList.toggle('on', next === 'url');
      segFile.classList.toggle('on', next === 'file');
      el(config.urlRowId).style.display = next === 'url' ? '' : 'none';
      el(config.fileRowId).style.display = next === 'file' ? 'flex' : 'none';
    }
    segUrl.onclick = () => setMode('url');
    segFile.onclick = () => setMode('file');
    setMode(mode);
  }

  cancelBtn.onclick = async () => {
    if (!activeJob) return;
    cancelBtn.disabled = true;
    try {
      const r = await fetch(baseUrl() + '/jobs/' + activeJob + '/cancel', {
        method: 'POST',
        headers: activeHeaders,
      });
      if (!r.ok) throw new Error(await r.text());
      append(t('ingest.cancelReq'));
    } catch (e) {
      append(t('ingest.cancelErr') + e.message);
      cancelBtn.disabled = false;
    }
  };

  el(config.runId).onclick = async () => {
    const url = baseUrl();
    const H = authHeaders();
    const opts = ingestOptions(
      kindInput.value,
      sectionHeadingInput.value,
    );
    log.textContent = '';
    try {
      const payload = sourcePayload(H, opts);
      if (payload.error) return append(payload.error);
      const res = await fetch(url + '/ingest', { method: 'POST', ...payload });
      if (!res.ok) throw new Error(await res.text());
      const { job_id: jobId } = await res.json();
      append(t('ingest.jobStarted', { id: jobId }));
      activeJob = jobId;
      activeHeaders = H;
      cancelBtn.style.display = '';
      cancelBtn.disabled = false;
      try {
        await streamJob(url, jobId, H, append);
      } finally {
        activeJob = null;
        activeHeaders = {};
        cancelBtn.style.display = 'none';
        cancelBtn.disabled = false;
      }
    } catch (e) {
      append(t('ingest.err') + e.message);
    }
  };

  ping();
  window.addEventListener('focus', () => { if (!backendOnline) ping(); });
}
