import { headersFor, streamJob } from './backend-client.js';

const STATUS = {
  online: ['● backend online', 'var(--sage)'],
  error: ['○ backend error', 'var(--text-3)'],
  offline: ['○ backend offline', 'var(--text-3)'],
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

export function mount(config) {
  const urlInput = el(config.urlId);
  const tokenInput = el(config.tokenId);
  const status = el(config.statusId);
  const log = el(config.logId);
  const cancelBtn = el(config.cancelId);
  const kindInput = el(config.kindId);
  const sectionHeadingInput = el(config.sectionHeadingId);
  let activeJob = null;
  let activeHeaders = {};
  let mode = 'url';

  function syncSectionSupport() {
    sectionHeadingInput.disabled = kindInput.value !== 'auto' && kindInput.value !== 'wiki';
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
      const [text, color] = r.ok ? STATUS.online : STATUS.error;
      status.textContent = text;
      status.style.color = color;
    } catch {
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
      append('cancel requested');
    } catch (e) {
      append('cancel error: ' + e.message);
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
      let res;
      if (mode === 'file') {
        const file = el(config.fileInputId).files[0];
        if (!file) return append('Choose a file first.');
        const fd = new FormData();
        fd.append('file', file);
        fd.append('options', JSON.stringify(opts));
        res = await fetch(url + '/ingest', { method: 'POST', headers: H, body: fd });
      } else {
        const target = el(config.urlInputId).value.trim();
        if (!target) return append('Enter a URL first.');
        res = await fetch(url + '/ingest', {
          method: 'POST',
          headers: { ...H, 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: target, options: opts }),
        });
      }
      if (!res.ok) throw new Error(await res.text());
      const { job_id: jobId } = await res.json();
      append('job ' + jobId + ' started');
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
      append('error: ' + e.message);
    }
  };

  ping();
}
