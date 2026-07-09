import { installSourceReaderAnchors } from './source-reader-anchors-dom.js';
import { esc } from './list-filter.js';

const ANNO_COLORS = new Set(['note', 'question', 'important']);
const DEFAULT_PREFS = { fontSize: 18, lineHeight: 1.9, width: '42rem', theme: 'night', font: 'serif', mode: 'scroll', focus: false };
const WIDTHS = new Set(['34rem', '42rem', '50rem']);
const THEMES = new Set(['night', 'paper', 'sepia']);
const FONTS = new Set(['serif', 'sans']);
const MODES = new Set(['scroll', 'page']);

const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));

function attr(s) { return esc(s).replace(/"/g, '&quot;'); }
function safeColor(c) { return ANNO_COLORS.has(c) ? c : 'note'; }
function safeWikiHref(href) { return typeof href === 'string' && href.startsWith('/wiki/') ? href : ''; }
function readJson(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) || fallback; } catch { return fallback; } }
function writeJson(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch {} }

function cleanPrefs(raw) {
  const p = { ...DEFAULT_PREFS, ...(raw || {}) };
  return {
    fontSize: clamp(Number(p.fontSize) || DEFAULT_PREFS.fontSize, 16, 24),
    lineHeight: clamp(Number(p.lineHeight) || DEFAULT_PREFS.lineHeight, 1.65, 2.25),
    width: WIDTHS.has(p.width) ? p.width : DEFAULT_PREFS.width,
    theme: THEMES.has(p.theme) ? p.theme : DEFAULT_PREFS.theme,
    font: FONTS.has(p.font) ? p.font : DEFAULT_PREFS.font,
    mode: MODES.has(p.mode) ? p.mode : DEFAULT_PREFS.mode,
    focus: !!p.focus,
  };
}

function parseReaderConfig(root) {
  try {
    return JSON.parse(root?.dataset?.sourceReader || '{}') || {};
  } catch {
    return {};
  }
}

export function bootSourceReaderFromDom() {
  const run = () => bootSourceReader(parseReaderConfig(document.getElementById('srcread')));
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run, { once: true });
  else run();
}

function bootSourceReader(options = {}) {
  installSourceReaderAnchors();

  const {
    sourceId = '',
    sourceTitle = '',
    promoteTargets = [],
    chapterCommands = [],
    chapterScoped = false,
  } = options;
  if (!sourceId) return;

  const anchors = window.SourceReaderAnchors;
  const root = document.getElementById('srcread');
  if (!root || root.dataset.readerBooted === 'true') return;
  root.dataset.readerBooted = 'true';

  const doc = document.getElementById('sr-doc');
  const pop = document.getElementById('sr-pop');
  const list = document.getElementById('sr-list');
  const orphans = document.getElementById('sr-orphans');
  const statusEl = document.getElementById('sr-status');
  const prefsPanel = document.getElementById('sr-prefs');
  const progressFill = document.querySelector('#sr-progress span');
  const progressText = document.getElementById('sr-progress-text');
  const notesEl = document.getElementById('sr-notes');
  if (!anchors || !doc || !pop || !list || !orphans || !statusEl) return;

  const BACKEND = localStorage.getItem('backendUrl') || 'http://localhost:8787';
  const TOKEN = localStorage.getItem('backendToken') || '';
  const H = { 'Content-Type': 'application/json', ...(TOKEN ? { 'X-Auth-Token': TOKEN } : {}) };
  const PREFS_KEY = 'sourceReaderPrefs:v1';
  const PROGRESS_KEY = `sourceReaderProgress:${sourceId}`;

  let annotations = [];
  let allAnnotationCount = 0;
  let otherChapterNoteCount = 0;
  let filter = 'all';
  let lastSel = null;
  let pendingRegion = null;
  let activeBlock = null;
  let saveProgressTimer = 0;
  let progressRaf = 0;
  let figDraw = null;
  let assistText = '';
  let cmdItems = [];
  let cmdCatalog = [];
  let cmdSel = 0;
  let prefs = cleanPrefs(readJson(PREFS_KEY, DEFAULT_PREFS));

  const renderedBlockIds = new Set([...doc.querySelectorAll('[data-block]')].map((el) => el.dataset.block).filter(Boolean));
  const renderedSectionIds = new Set([...doc.querySelectorAll('[data-section]')].map((el) => el.dataset.section).filter(Boolean));

  function isPopoverOpen(el) {
    return !!el?.matches?.(':popover-open');
  }

  function showPopover(el) {
    if (!el || isPopoverOpen(el)) return;
    if (typeof el.showPopover === 'function') el.showPopover();
    else el.hidden = false;
  }

  function hidePopover(el) {
    if (!el) return;
    if (typeof el.hidePopover === 'function') {
      if (isPopoverOpen(el)) el.hidePopover();
    } else {
      el.hidden = true;
    }
  }

  function inReaderScope(annotation) {
    if (!chapterScoped) return true;
    const target = annotation?.target || {};
    if (target.block_id) return renderedBlockIds.has(target.block_id);
    if (target.section_id) return renderedSectionIds.has(target.section_id);
    return true;
  }

  function annotationStatus(fuzzy = 0, orphanCount = 0) {
    return `${annotations.length} note(s)`
      + (fuzzy ? ` · ${fuzzy} re-anchored` : '')
      + (orphanCount ? ` · ${orphanCount} orphaned` : '')
      + (otherChapterNoteCount ? ` · ${otherChapterNoteCount} in other chapters` : '');
  }

  function syncPrefsControls() {
    if (!prefsPanel) return;
    const fs = prefsPanel.querySelector('[data-pref="fontSize"]');
    const lh = prefsPanel.querySelector('[data-pref="lineHeight"]');
    if (fs) fs.value = prefs.fontSize;
    if (lh) lh.value = prefs.lineHeight;
    prefsPanel.querySelector('[data-out="fontSize"]').textContent = `${prefs.fontSize}px`;
    prefsPanel.querySelector('[data-out="lineHeight"]').textContent = prefs.lineHeight.toFixed(2);
    prefsPanel.querySelectorAll('[data-width]').forEach((b) => b.classList.toggle('on', b.dataset.width === prefs.width));
    prefsPanel.querySelectorAll('[data-theme]').forEach((b) => b.classList.toggle('on', b.dataset.theme === prefs.theme));
    prefsPanel.querySelectorAll('[data-font]').forEach((b) => b.classList.toggle('on', b.dataset.font === prefs.font));
    prefsPanel.querySelectorAll('[data-mode]').forEach((b) => b.classList.toggle('on', b.dataset.mode === prefs.mode));
    document.querySelector('[data-act="focus"]')?.setAttribute('aria-pressed', String(prefs.focus));
  }

  function savePrefs() { writeJson(PREFS_KEY, prefs); }

  function applyPrefs() {
    root.style.setProperty('--reader-font-size', `${prefs.fontSize}px`);
    root.style.setProperty('--reader-line-height', String(prefs.lineHeight));
    root.style.setProperty('--reader-width', prefs.width);
    root.dataset.theme = prefs.theme;
    root.dataset.font = prefs.font;
    root.dataset.mode = prefs.mode;
    document.body.classList.toggle('sr-reader-page', prefs.mode === 'page');
    document.body.classList.toggle('sr-reader-focus', prefs.focus);
    syncPrefsControls();
  }

  function setPrefsOpen(open) {
    const btn = document.querySelector('[data-act="prefs"]');
    if (!prefsPanel) return;
    if (open) showPopover(prefsPanel);
    else hidePopover(prefsPanel);
    btn?.setAttribute('aria-expanded', String(!!open));
  }

  function setFocusMode(on) {
    prefs.focus = !!on;
    applyPrefs();
    savePrefs();
  }

  function pageStep(direction) {
    const distance = Math.max(320, Math.round(window.innerHeight * 0.82));
    window.scrollBy({ top: direction * distance, behavior: 'smooth' });
  }

  function isDrawerViewport() { return window.matchMedia('(max-width: 900px)').matches; }

  function setNotesOpen(open) {
    document.body.classList.toggle('sr-notes-open', !!open);
    document.querySelector('[data-act="notes"]')?.setAttribute('aria-expanded', String(!!open));
  }

  prefsPanel?.addEventListener('input', (e) => {
    const input = e.target.closest('[data-pref]');
    if (!input) return;
    if (input.dataset.pref === 'fontSize') prefs.fontSize = clamp(Number(input.value), 16, 24);
    if (input.dataset.pref === 'lineHeight') prefs.lineHeight = clamp(Number(input.value), 1.65, 2.25);
    applyPrefs();
    savePrefs();
  });

  prefsPanel?.addEventListener('click', (e) => {
    const target = e.target instanceof Element ? e.target : e.target?.parentElement;
    const button = target?.closest('button');
    if (!button || !prefsPanel.contains(button)) return;
    if (button.dataset.act === 'prefs-reset') prefs = { ...DEFAULT_PREFS };
    else if (button.dataset.width) prefs.width = button.dataset.width;
    else if (button.dataset.theme) prefs.theme = button.dataset.theme;
    else if (button.dataset.font) prefs.font = button.dataset.font;
    else if (button.dataset.mode) prefs.mode = button.dataset.mode;
    else return;
    applyPrefs();
    savePrefs();
  });
  prefsPanel?.addEventListener('toggle', () => {
    document.querySelector('[data-act="prefs"]')?.setAttribute('aria-expanded', String(isPopoverOpen(prefsPanel)));
  });

  document.getElementById('sr-notes-shade')?.addEventListener('click', () => setNotesOpen(false));
  document.querySelector('[data-act="notes-close"]')?.addEventListener('click', () => setNotesOpen(false));
  root.querySelector('.sr-actions')?.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn) return;
    if (btn.dataset.act === 'prefs') setPrefsOpen(!isPopoverOpen(prefsPanel));
    if (btn.dataset.act === 'focus') setFocusMode(!prefs.focus);
    if (btn.dataset.act === 'page-prev') pageStep(-1);
    if (btn.dataset.act === 'page-next') pageStep(1);
    if (btn.dataset.act === 'notes') {
      if (isDrawerViewport()) setNotesOpen(!document.body.classList.contains('sr-notes-open'));
      else notesEl?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    }
    if (btn.dataset.act === 'resume') restoreProgress(true);
  });

  function scrollPercent() {
    const max = Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
    return clamp((window.scrollY / max) * 100, 0, 100);
  }

  function sectionLabelFor(el) {
    let n = el;
    while (n && !n.classList?.contains('sec-h')) n = n.previousElementSibling;
    const label = (n?.textContent || '').trim();
    return label.length > 18 ? label.slice(0, 18) + '...' : label;
  }

  function updateProgressUi(el = activeBlock) {
    const pct = scrollPercent();
    if (progressFill) progressFill.style.width = `${pct}%`;
    if (progressText) {
      const label = el ? sectionLabelFor(el) : '';
      progressText.textContent = `${Math.round(pct)}%${label ? ` · ${label}` : ''}`;
    }
  }

  function updateResumeState() {
    const btn = document.querySelector('[data-act="resume"]');
    if (!btn) return;
    const saved = readJson(PROGRESS_KEY, null);
    btn.disabled = !saved?.block_id;
  }

  function saveProgress(el = activeBlock) {
    if (!el?.dataset?.block) return;
    writeJson(PROGRESS_KEY, {
      block_id: el.dataset.block,
      section_id: el.dataset.section || '',
      scrollY: Math.round(window.scrollY),
      percent: Math.round(scrollPercent()),
      updated: Date.now(),
    });
    updateResumeState();
  }

  function queueProgressSave(el = activeBlock) {
    if (saveProgressTimer) return;
    saveProgressTimer = window.setTimeout(() => {
      saveProgressTimer = 0;
      saveProgress(el);
    }, 700);
  }

  function restoreProgress(animate) {
    const saved = readJson(PROGRESS_KEY, null);
    const el = saved?.block_id ? doc.querySelector(`[data-block="${CSS.escape(saved.block_id)}"]`) : null;
    if (!el) return false;
    activeBlock = el;
    el.scrollIntoView({ block: 'center', behavior: animate ? 'smooth' : 'auto' });
    updateProgressUi(el);
    if (animate) {
      el.classList.add('pulse');
      setTimeout(() => el.classList.remove('pulse'), 1500);
    }
    return true;
  }

  const progressObserver = new IntersectionObserver((entries) => {
    const hit = entries
      .filter((entry) => entry.isIntersecting)
      .sort((a, b) => Math.abs(a.boundingClientRect.top - 120) - Math.abs(b.boundingClientRect.top - 120))[0];
    if (!hit) return;
    activeBlock = hit.target;
    updateProgressUi(activeBlock);
    queueProgressSave(activeBlock);
  }, { rootMargin: '-12% 0px -76% 0px', threshold: [0, 0.1, 0.5] });

  doc.querySelectorAll('.blk, .fig, .sub-h').forEach((el) => progressObserver.observe(el));
  window.addEventListener('scroll', () => {
    if (progressRaf) return;
    progressRaf = requestAnimationFrame(() => {
      progressRaf = 0;
      updateProgressUi(activeBlock);
      queueProgressSave(activeBlock);
    });
  }, { passive: true });
  window.addEventListener('beforeunload', () => saveProgress(activeBlock));
  applyPrefs();
  updateResumeState();
  updateProgressUi();

  async function load() {
    try {
      const r = await fetch(`${BACKEND}/annotations?source_id=${encodeURIComponent(sourceId)}`, { headers: H });
      if (r.status === 503) {
        statusEl.textContent = 'Set PW_AUTH_TOKEN on the backend to use annotations.';
        return;
      }
      if (!r.ok) throw new Error();
      const loaded = await r.json();
      allAnnotationCount = loaded.length;
      annotations = loaded.filter(inReaderScope);
      otherChapterNoteCount = chapterScoped ? Math.max(0, allAnnotationCount - annotations.length) : 0;
      statusEl.textContent = annotationStatus();
      render();
    } catch {
      statusEl.textContent = 'Study backend offline — start it to load/save notes.';
    }
  }

  function resolveBlock(a) {
    return anchors.resolveBlock(doc, a);
  }

  function charRange(rootEl, start, end) {
    const r = document.createRange();
    let pos = 0;
    let f = 0;
    const w = document.createTreeWalker(rootEl, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      const len = n.nodeValue.length;
      if (!(f & 1) && start <= pos + len) {
        r.setStart(n, start - pos);
        f |= 1;
      }
      if (!(f & 2) && end <= pos + len) {
        r.setEnd(n, end - pos);
        f |= 2;
        break;
      }
      pos += len;
    }
    return f === 3 ? r : null;
  }

  function clearMarks() {
    doc.querySelectorAll('mark.anno').forEach((m) => {
      const p = m.parentNode;
      while (m.firstChild) p.insertBefore(m.firstChild, m);
      p.removeChild(m);
      p.normalize();
    });
  }

  function clearRegions() {
    doc.querySelectorAll('.anno-region').forEach((x) => x.remove());
  }

  function fuzzyLocate(a, preferredEl) {
    return anchors.fuzzyLocate(doc, a, preferredEl);
  }

  function render() {
    clearMarks();
    clearRegions();
    const orphan = [];
    let fuzzy = 0;
    for (const a of annotations) {
      const el = resolveBlock(a);
      const sel = a.target?.selector || {};
      if (el && el.classList.contains('fig')) {
        const wrap = el.querySelector('.fig-imgwrap');
        if (wrap) {
          const box = document.createElement('div');
          box.className = `anno-region ${safeColor(a.color)}` + (sel.region ? '' : ' whole');
          box.dataset.aid = a.id;
          const rg = sel.region || { x: 0.02, y: 0.02, w: 0.96, h: 0.96 };
          box.style.left = (rg.x * 100) + '%';
          box.style.top = (rg.y * 100) + '%';
          box.style.width = (rg.w * 100) + '%';
          box.style.height = (rg.h * 100) + '%';
          wrap.appendChild(box);
        } else {
          orphan.push(a);
        }
        continue;
      }
      let hit = null;
      let isFuzzy = false;
      if (sel.quote != null && sel.quote !== '') {
        if (el) {
          const text = el.textContent;
          const exact = anchors.locateQuote(text, sel);
          if (exact) hit = { el, start: exact.start, end: exact.end };
        }
        if (!hit) {
          hit = fuzzyLocate(a, el);
          if (hit) isFuzzy = true;
        }
      }
      let placed = false;
      if (hit) {
        const rng = charRange(hit.el, hit.start, hit.end);
        if (rng) {
          try {
            const m = document.createElement('mark');
            m.className = `anno ${safeColor(a.color)}${isFuzzy ? ' fuzzy' : ''}`;
            m.dataset.aid = a.id;
            if (isFuzzy) m.title = 'Re-anchored — the source text changed slightly since this note';
            rng.surroundContents(m);
            placed = true;
            if (isFuzzy) fuzzy++;
          } catch {}
        }
      }
      if (!placed) orphan.push(a);
    }
    statusEl.textContent = annotationStatus(fuzzy, orphan.length);
    renderRail(orphan);
  }

  function renderRail(orphan) {
    const countEl = document.getElementById('sr-count');
    if (countEl) {
      countEl.textContent = chapterScoped && allAnnotationCount
        ? `(${annotations.length}/${allAnnotationCount})`
        : (annotations.length ? `(${annotations.length})` : '');
    }
    const show = annotations.filter((a) => filter === 'all' || safeColor(a.color) === filter);
    list.innerHTML = show.map((a) => {
      let q = a.target?.selector?.quote || '';
      if (!q && (a.target?.block_id || '').startsWith('i-')) q = a.target?.selector?.region ? '🖼 figure region' : '🖼 figure';
      const promo = (a.links || []).find((l) => l && l.type === 'human-zone');
      const href = promo ? safeWikiHref(promo.href) : '';
      const promoted = href ? `<a class="promoted" href="${attr(href)}" title="In ${attr(promo.wiki_rel)}">⇪ ${esc((promo.wiki_rel || '').split('/').pop())}</a>` : '';
      const aid = attr(a.id);
      return `<div class="anno-card ${safeColor(a.color)}" data-aid="${aid}">
        <div class="q">“${esc(q.slice(0, 60))}”</div>
        <div class="b" contenteditable="true" data-aid="${aid}">${esc(a.body || '')}</div>
        <div class="meta"><span>${esc((a.updated || '').slice(5, 10))}</span>${promoted}<button class="promote-btn" data-promote="${aid}" title="Promote into a wiki page's human-zone">⇪</button><button data-del="${aid}" title="Delete">✕</button></div>
      </div>`;
    }).join('') || `<p class="small muted">${chapterScoped ? 'Select text in this chapter' : 'Select text in the source'} to highlight or comment.</p>`;
    orphans.innerHTML = orphan.length
      ? `<div style="margin-top:12px"><h4 style="color:var(--amber)">Orphaned (${orphan.length})</h4>${orphan.map((a) => `<div class="anno-card ${safeColor(a.color)}"><div class="q">“${esc((a.target?.selector?.quote || '').slice(0, 50))}”</div><div class="b">${esc(a.body || '')}</div><div class="meta"><span>text moved</span><button data-del="${attr(a.id)}">✕</button></div></div>`).join('')}</div>`
      : '';
  }

  function openAnnotationPopover(x, y, { copy = true, ai = true } = {}) {
    const cb = pop.querySelector('[data-act="copy"]');
    const ab = pop.querySelector('[data-act="ai"]');
    if (cb) cb.hidden = !copy;
    if (ab) ab.hidden = !ai;
    showPopover(pop);
    pop.style.left = Math.max(8, Math.min(x - pop.offsetWidth / 2, window.innerWidth - pop.offsetWidth - 8)) + 'px';
    pop.style.top = Math.max(8, y - pop.offsetHeight - 8) + 'px';
  }

  doc.addEventListener('mouseup', (e) => {
    if (e.target.closest && e.target.closest('.fig-imgwrap')) return;
    const s = window.getSelection();
    if (!s || s.isCollapsed || !s.toString().trim()) {
      hidePopover(pop);
      return;
    }
    const range = s.getRangeAt(0);
    const blockEl = (range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement).closest('.blk');
    if (!blockEl || !blockEl.contains(range.endContainer)) {
      hidePopover(pop);
      return;
    }
    const pre = range.cloneRange();
    pre.selectNodeContents(blockEl);
    pre.setEnd(range.startContainer, range.startOffset);
    const start = pre.toString().length;
    const quote = range.toString();
    const end = start + quote.length;
    const text = blockEl.textContent;
    lastSel = {
      block_id: blockEl.dataset.block,
      section_id: blockEl.dataset.section,
      prev_block_id: blockEl.dataset.prev,
      next_block_id: blockEl.dataset.next,
      quote,
      prefix: text.slice(Math.max(0, start - 32), start),
      suffix: text.slice(end, end + 32),
      start,
      end,
    };
    pendingRegion = null;
    const rect = range.getBoundingClientRect();
    openAnnotationPopover(rect.left + rect.width / 2, rect.top, { copy: true, ai: true });
  });

  async function saveAnnotation(target, color, focusNote) {
    try {
      const r = await fetch(`${BACKEND}/annotations`, { method: 'POST', headers: H, body: JSON.stringify({ source_id: sourceId, color, target, body: '' }) });
      if (!r.ok) throw new Error(await r.text());
      const a = await r.json();
      allAnnotationCount++;
      annotations.push(a);
      render();
      if (focusNote) {
        const b = list.querySelector(`.b[data-aid="${CSS.escape(a.id)}"]`);
        if (b) b.focus();
      }
    } catch (e) {
      alert('Could not save (backend offline or no token?).\n' + e.message);
    }
  }

  function create(color, focusNote) {
    if (!lastSel) return;
    hidePopover(pop);
    window.getSelection().removeAllRanges();
    saveAnnotation({
      block_id: lastSel.block_id,
      section_id: lastSel.section_id,
      context: { prev_block_id: lastSel.prev_block_id, next_block_id: lastSel.next_block_id },
      selector: { quote: lastSel.quote, prefix: lastSel.prefix, suffix: lastSel.suffix, start: lastSel.start, end: lastSel.end },
    }, color, focusNote);
  }

  function createRegion(fig, region, color, focusNote) {
    saveAnnotation({
      block_id: fig.dataset.block,
      section_id: fig.dataset.section,
      context: { prev_block_id: fig.dataset.prev, next_block_id: fig.dataset.next },
      selector: region ? { quote: '', region } : { quote: '' },
    }, color || 'note', focusNote);
  }

  function commitAnno(color, focusNote) {
    if (pendingRegion) {
      const { fig, region } = pendingRegion;
      pendingRegion = null;
      hidePopover(pop);
      createRegion(fig, region, color, focusNote);
    } else {
      create(color, focusNote);
    }
  }

  pop.querySelectorAll('.dot').forEach((d) => d.addEventListener('click', () => commitAnno(d.dataset.color, false)));
  pop.querySelector('[data-act="comment"]').addEventListener('click', () => commitAnno('note', true));
  pop.querySelector('[data-act="copy"]').addEventListener('click', () => {
    if (lastSel) navigator.clipboard?.writeText(lastSel.quote);
    hidePopover(pop);
  });
  pop.querySelector('[data-act="ai"]').addEventListener('click', () => {
    if (lastSel) {
      openAssist(lastSel.quote);
      hidePopover(pop);
    }
  });

  function openFigPopover(fig, region, cx, cy) {
    pendingRegion = { fig, region };
    lastSel = null;
    openAnnotationPopover(cx, cy, { copy: false, ai: false });
  }

  doc.addEventListener('mousedown', (e) => {
    const wrap = e.target.closest('.fig-imgwrap');
    if (!wrap || e.target.closest('.fig-add') || e.target.closest('.anno-region')) return;
    figDraw = { wrap, fig: wrap.closest('.fig'), rect: wrap.getBoundingClientRect(), x0: e.clientX, y0: e.clientY, rub: null };
    wrap.classList.add('drawing');
    e.preventDefault();
  });

  window.addEventListener('mousemove', (e) => {
    if (!figDraw) return;
    const { rect, wrap } = figDraw;
    const x = Math.min(e.clientX, figDraw.x0);
    const y = Math.min(e.clientY, figDraw.y0);
    const w = Math.abs(e.clientX - figDraw.x0);
    const h = Math.abs(e.clientY - figDraw.y0);
    if (!figDraw.rub) {
      figDraw.rub = document.createElement('div');
      figDraw.rub.className = 'fig-rubber';
      wrap.appendChild(figDraw.rub);
    }
    figDraw.rub.style.left = (x - rect.left) + 'px';
    figDraw.rub.style.top = (y - rect.top) + 'px';
    figDraw.rub.style.width = w + 'px';
    figDraw.rub.style.height = h + 'px';
  });

  window.addEventListener('mouseup', (e) => {
    if (!figDraw) return;
    const dd = figDraw;
    figDraw = null;
    dd.wrap.classList.remove('drawing');
    if (dd.rub) dd.rub.remove();
    const w = Math.abs(e.clientX - dd.x0);
    const h = Math.abs(e.clientY - dd.y0);
    if (w < 6 || h < 6) return;
    const rect = dd.rect;
    openFigPopover(dd.fig, {
      x: Math.max(0, (Math.min(e.clientX, dd.x0) - rect.left) / rect.width),
      y: Math.max(0, (Math.min(e.clientY, dd.y0) - rect.top) / rect.height),
      w: Math.min(1, w / rect.width),
      h: Math.min(1, h / rect.height),
    }, e.clientX, e.clientY);
  });

  list.addEventListener('blur', async (e) => {
    const b = e.target.closest('.b');
    if (!b) return;
    const aid = b.dataset.aid;
    const body = b.textContent;
    const a = annotations.find((x) => x.id === aid);
    if (!a || a.body === body) return;
    a.body = body;
    try {
      await fetch(`${BACKEND}/annotations/${aid}`, { method: 'PATCH', headers: H, body: JSON.stringify({ body }) });
    } catch {}
  }, true);

  function closePromoteMenu() {
    const menu = document.getElementById('sr-pmenu');
    if (!menu) return;
    if (typeof menu.hidePopover === 'function' && isPopoverOpen(menu)) hidePopover(menu);
    else menu.remove();
  }

  function openPromoteMenu(aid, btn) {
    closePromoteMenu();
    const menu = document.createElement('div');
    menu.id = 'sr-pmenu';
    menu.popover = 'auto';
    menu.innerHTML = '<div class="ph">Promote to human-zone</div>'
      + (promoteTargets || []).map((t) => `<button data-rel="${attr(t.rel)}">${esc(t.title || t.rel)}</button>`).join('')
      + '<button data-rel="__custom__" class="custom">Other page…</button>';
    menu.style.visibility = 'hidden';
    menu.addEventListener('toggle', () => {
      if (!isPopoverOpen(menu)) menu.remove();
    });
    document.body.appendChild(menu);
    showPopover(menu);
    const r = btn.getBoundingClientRect();
    menu.style.left = Math.max(8, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8)) + 'px';
    menu.style.top = (r.bottom + 4) + 'px';
    menu.style.visibility = '';
    menu.addEventListener('click', (ev) => {
      const b = ev.target.closest('button');
      if (!b) return;
      let rel = b.dataset.rel;
      if (rel === '__custom__') {
        rel = prompt('Wiki page path (e.g. entities/ATP):', '');
        if (!rel) {
          closePromoteMenu();
          return;
        }
      }
      closePromoteMenu();
      doPromote(aid, rel.trim());
    });
  }

  async function doPromote(aid, rel) {
    if (!rel) return;
    try {
      const r = await fetch(`${BACKEND}/annotations/${aid}/promote`, { method: 'POST', headers: H, body: JSON.stringify({ wiki_rel: rel, source_title: sourceTitle }) });
      if (!r.ok) throw new Error(await r.text());
      const res = await r.json();
      const a = annotations.find((x) => x.id === aid);
      if (a && res.annotation) a.links = res.annotation.links;
      statusEl.textContent = res.committed ? `promoted → ${rel} ✓` : `promoted → ${rel} (already up to date)`;
      render();
    } catch (e) {
      alert('Promote failed (backend offline or bad page path?).\n' + e.message);
    }
  }

  document.addEventListener('click', async (e) => {
    const pb = e.target.closest('[data-promote]');
    if (pb) {
      openPromoteMenu(pb.dataset.promote, pb);
      return;
    }
    const add = e.target.closest('.fig-add');
    if (add) {
      createRegion(add.closest('.fig'), null, 'note', true);
      return;
    }
    const del = e.target.closest('[data-del]');
    if (del) {
      const aid = del.dataset.del;
      try {
        await fetch(`${BACKEND}/annotations/${aid}`, { method: 'DELETE', headers: H });
      } catch {}
      annotations = annotations.filter((x) => x.id !== aid);
      allAnnotationCount = Math.max(0, allAnnotationCount - 1);
      statusEl.textContent = annotationStatus();
      render();
      return;
    }
    const card = e.target.closest('.anno-card');
    if (card && card.dataset.aid) focusNote(card.dataset.aid);
    const mk = e.target.closest('mark.anno, .anno-region');
    if (mk) {
      const c = list.querySelector(`.anno-card[data-aid="${CSS.escape(mk.dataset.aid)}"]`);
      if (isDrawerViewport()) setNotesOpen(true);
      if (c) c.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  });

  function focusNote(id) {
    const sid = CSS.escape(id);
    const m = doc.querySelector(`mark[data-aid="${sid}"]`) || doc.querySelector(`.anno-region[data-aid="${sid}"]`);
    doc.querySelectorAll('.focus').forEach((x) => x.classList.remove('focus'));
    if (m) {
      m.scrollIntoView({ block: 'center', behavior: 'smooth' });
      m.classList.add('focus');
    }
    const c = list.querySelector(`.anno-card[data-aid="${sid}"]`);
    if (c) c.scrollIntoView({ block: 'center' });
  }

  function setFilter(f) {
    filter = f;
    document.querySelectorAll('#sr-chips button').forEach((x) => x.classList.toggle('on', x.dataset.f === f));
    render();
  }

  document.getElementById('sr-chips').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (b) setFilter(b.dataset.f);
  });

  function pulse(el) {
    if (!el) return;
    el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    el.classList.add('pulse');
    activeBlock = el.matches?.('[data-block]') ? el : el.querySelector?.('[data-block]') || activeBlock;
    updateProgressUi(activeBlock);
    queueProgressSave(activeBlock);
    setTimeout(() => el.classList.remove('pulse'), 1500);
  }

  function locate(anchor) {
    if (!anchor) return null;
    let el = null;
    if (anchor.startsWith('p-') || anchor.startsWith('h-')) el = doc.querySelector(`[data-block="${CSS.escape(anchor)}"]`);
    else if (anchor.startsWith('s-')) el = doc.querySelector(`[data-section="${CSS.escape(anchor)}"]`);
    else el = [...doc.querySelectorAll('.sec-h')].find((s) => s.textContent === anchor) || doc.querySelector(`[data-block="${CSS.escape(anchor)}"]`);
    pulse(el);
    return el;
  }

  document.getElementById('sr-cited')?.addEventListener('click', (e) => {
    if (e.target.closest('a')) return;
    const card = e.target.closest('.cite-card');
    if (!card || !card.dataset.anchor) return;
    locate(card.dataset.anchor);
  });

  function resolveFragment() {
    const h = decodeURIComponent(location.hash.slice(1));
    if (!h) return;
    if (h.startsWith('b=') || h.includes('&')) {
      const q = new URLSearchParams(h);
      pulse(resolveBlock({ target: { block_id: q.get('b'), section_id: q.get('s'), context: { prev_block_id: q.get('prev') || '', next_block_id: q.get('next') || '' } } }));
    } else if (h.startsWith('s=')) {
      locate(h.slice(2));
    } else if (h.startsWith('sec=')) {
      locate(h.slice(4));
    } else if (h.startsWith('s-')) {
      locate(h);
    } else if (h.startsWith('p-') || h.startsWith('h-')) {
      locate(h);
    }
  }

  const assist = document.getElementById('sr-assist');

  async function runAssist(mode) {
    assist.querySelectorAll('.modes button').forEach((b) => b.classList.toggle('on', b.dataset.m === mode));
    const out = assist.querySelector('.out');
    out.textContent = '…thinking';
    try {
      const r = await fetch(`${BACKEND}/assist`, { method: 'POST', headers: H, body: JSON.stringify({ text: assistText, mode }) });
      const j = await r.json();
      out.textContent = j.result || '(no result)';
    } catch {
      out.textContent = 'AI assist unavailable — is the backend running?';
    }
  }

  function openAssist(text) {
    assistText = (text || '').trim();
    if (!assistText) return;
    assist.querySelector('.src-q').textContent = '“' + assistText.slice(0, 160) + (assistText.length > 160 ? '…' : '') + '”';
    showPopover(assist);
    runAssist('explain');
  }

  assist.querySelector('.x').addEventListener('click', () => {
    hidePopover(assist);
  });
  assist.querySelectorAll('.modes button').forEach((b) => b.addEventListener('click', () => runAssist(b.dataset.m)));

  const cmd = document.getElementById('sr-cmd');
  const cmdInput = cmd.querySelector('input');
  const cmdList = cmd.querySelector('.cmd-list');

  function buildCommands() {
    const c = [];
    c.push({ label: 'Reader · resume position', run: () => restoreProgress(true) });
    c.push({ label: 'Reader · previous page', run: () => pageStep(-1) });
    c.push({ label: 'Reader · next page', run: () => pageStep(1) });
    c.push({ label: prefs.focus ? 'Reader · exit focus mode' : 'Reader · focus mode', run: () => setFocusMode(!prefs.focus) });
    c.push({ label: 'Reader · preferences', run: () => setPrefsOpen(true) });
    if (chapterCommands?.length) chapterCommands.forEach((chapter) => c.push({ label: 'Chapter · ' + chapter.title, run: () => { location.href = chapter.href; } }));
    else [...doc.querySelectorAll('.sec-h')].forEach((s) => c.push({ label: 'Chapter · ' + s.textContent, run: () => locate(s.textContent) }));
    [['all', 'All notes'], ['note', 'Notes'], ['question', 'Questions'], ['important', 'Important']].forEach(([f, l]) => c.push({ label: 'Filter · ' + l, run: () => setFilter(f) }));
    c.push({ label: 'Toggle · expand all citation groups', run: () => document.querySelectorAll('#sr-cited details').forEach((d) => d.open = true) });
    if (lastSel && lastSel.quote) c.push({ label: '✦ Ask AI about the selection', run: () => openAssist(lastSel.quote) });
    annotations.forEach((a) => {
      const q = a.target?.selector?.quote || ((a.target?.block_id || '').startsWith('i-') ? '🖼 figure' : '');
      c.push({ label: 'Note · ' + (q || '(note)').slice(0, 44), run: () => focusNote(a.id) });
    });
    return c;
  }

  function renderCmd(q) {
    const needle = q.toLowerCase();
    cmdItems = needle ? cmdCatalog.filter((c) => c.label.toLowerCase().includes(needle)) : cmdCatalog;
    cmdSel = 0;
    cmdList.innerHTML = cmdItems.slice(0, 60).map((c, i) => `<div class="cmd-item${i === 0 ? ' sel' : ''}" data-i="${i}">${esc(c.label)}</div>`).join('') || '<div class="cmd-empty">No matches</div>';
  }

  function openCmd() {
    cmdCatalog = buildCommands();
    cmdInput.value = '';
    renderCmd('');
    if (!cmd.open) cmd.showModal();
    cmdInput.focus();
  }

  function closeCmd() {
    if (cmd.open) cmd.close();
  }

  cmd.addEventListener('close', () => {
    cmdItems = [];
    cmdCatalog = [];
  });

  function moveCmd(d) {
    const items = cmdList.querySelectorAll('.cmd-item');
    if (!items.length) return;
    items[cmdSel]?.classList.remove('sel');
    cmdSel = (cmdSel + d + items.length) % items.length;
    items[cmdSel].classList.add('sel');
    items[cmdSel].scrollIntoView({ block: 'nearest' });
  }

  cmdInput.addEventListener('input', () => renderCmd(cmdInput.value));
  cmd.addEventListener('click', (e) => {
    const it = e.target.closest('.cmd-item');
    if (it) {
      cmdItems[+it.dataset.i]?.run();
      closeCmd();
    } else if (e.target === cmd) {
      closeCmd();
    }
  });

  function cycleNote(dir) {
    const marks = [...doc.querySelectorAll('mark.anno, .anno-region')];
    if (!marks.length) return;
    cycleNote._i = ((cycleNote._i ?? -1) + dir + marks.length) % marks.length;
    focusNote(marks[cycleNote._i].dataset.aid);
  }

  window.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      e.stopImmediatePropagation();
      cmd.open ? closeCmd() : openCmd();
      return;
    }
    if (cmd.open) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        moveCmd(1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        moveCmd(-1);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        cmdItems[cmdSel]?.run();
        closeCmd();
      }
      return;
    }
    const inField = /INPUT|TEXTAREA/.test(document.activeElement?.tagName || '') || document.activeElement?.isContentEditable;
    if (inField) return;
    if (e.key === 'Escape') {
      setNotesOpen(false);
      return;
    }
    if (prefs.mode === 'page' && !e.metaKey && !e.ctrlKey && !e.altKey) {
      if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') {
        e.preventDefault();
        pageStep(1);
        return;
      }
      if (e.key === 'ArrowLeft' || e.key === 'PageUp') {
        e.preventDefault();
        pageStep(-1);
        return;
      }
    }
    const sel = window.getSelection();
    const hasSel = sel && !sel.isCollapsed && sel.toString().trim();
    if (e.key === 'h' && hasSel && lastSel) create('important', false);
    else if (e.key === 'c' && hasSel && lastSel) create('note', true);
    else if (e.key.toLowerCase() === 'a' && hasSel && lastSel) openAssist(lastSel.quote);
    else if (e.key === 'j') cycleNote(1);
    else if (e.key === 'k') cycleNote(-1);
  }, true);

  load().then(() => setTimeout(() => {
    if (location.hash) resolveFragment();
    else restoreProgress(false);
  }, 120));
  window.addEventListener('hashchange', resolveFragment);
}
