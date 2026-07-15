const DEFAULT_PREFS = { fontSize: 18, lineHeight: 1.9, width: '42rem', theme: 'night', font: 'serif', mode: 'scroll', focus: false };
const WIDTHS = new Set(['34rem', '42rem', '50rem']);
const THEMES = new Set(['night', 'paper', 'sepia']);
const FONTS = new Set(['serif', 'sans']);
const MODES = new Set(['scroll', 'page']);

export const clamp = (number, low, high) => Math.min(high, Math.max(low, number));

function readJson(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) || fallback; } catch { return fallback; }
}

function writeJson(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch {}
}

function cleanPrefs(raw) {
  const prefs = { ...DEFAULT_PREFS, ...(raw || {}) };
  return {
    fontSize: clamp(Number(prefs.fontSize) || DEFAULT_PREFS.fontSize, 16, 24),
    lineHeight: clamp(Number(prefs.lineHeight) || DEFAULT_PREFS.lineHeight, 1.65, 2.25),
    width: WIDTHS.has(prefs.width) ? prefs.width : DEFAULT_PREFS.width,
    theme: THEMES.has(prefs.theme) ? prefs.theme : DEFAULT_PREFS.theme,
    font: FONTS.has(prefs.font) ? prefs.font : DEFAULT_PREFS.font,
    mode: MODES.has(prefs.mode) ? prefs.mode : DEFAULT_PREFS.mode,
    focus: !!prefs.focus,
  };
}

export function installReaderState(root, doc, sourceId, popovers) {
  const prefsPanel = document.getElementById('sr-prefs');
  const progressFill = document.querySelector('#sr-progress span');
  const progressText = document.getElementById('sr-progress-text');
  const prefsKey = 'sourceReaderPrefs:v1';
  const progressKey = `sourceReaderProgress:${sourceId}`;
  const prefs = cleanPrefs(readJson(prefsKey, DEFAULT_PREFS));
  let activeBlock = null;
  let saveTimer = 0;
  let progressFrame = 0;

  function syncPrefsControls() {
    if (!prefsPanel) return;
    const fontSize = prefsPanel.querySelector('[data-pref="fontSize"]');
    const lineHeight = prefsPanel.querySelector('[data-pref="lineHeight"]');
    if (fontSize) fontSize.value = prefs.fontSize;
    if (lineHeight) lineHeight.value = prefs.lineHeight;
    prefsPanel.querySelector('[data-out="fontSize"]').textContent = `${prefs.fontSize}px`;
    prefsPanel.querySelector('[data-out="lineHeight"]').textContent = prefs.lineHeight.toFixed(2);
    prefsPanel.querySelectorAll('[data-width]').forEach((button) => button.classList.toggle('on', button.dataset.width === prefs.width));
    prefsPanel.querySelectorAll('[data-theme]').forEach((button) => button.classList.toggle('on', button.dataset.theme === prefs.theme));
    prefsPanel.querySelectorAll('[data-font]').forEach((button) => button.classList.toggle('on', button.dataset.font === prefs.font));
    prefsPanel.querySelectorAll('[data-mode]').forEach((button) => button.classList.toggle('on', button.dataset.mode === prefs.mode));
    document.querySelector('[data-act="focus"]')?.setAttribute('aria-pressed', String(prefs.focus));
  }

  function savePrefs() { writeJson(prefsKey, prefs); }

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
    const button = document.querySelector('[data-act="prefs"]');
    if (!prefsPanel) return;
    if (open) popovers.show(prefsPanel);
    else popovers.hide(prefsPanel);
    button?.setAttribute('aria-expanded', String(!!open));
  }

  function setFocusMode(on) {
    prefs.focus = !!on;
    applyPrefs();
    savePrefs();
  }

  function pageStep(direction) {
    window.scrollBy({ top: direction * Math.max(320, Math.round(window.innerHeight * 0.82)), behavior: 'smooth' });
  }

  prefsPanel?.addEventListener('input', (event) => {
    const input = event.target.closest('[data-pref]');
    if (!input) return;
    if (input.dataset.pref === 'fontSize') prefs.fontSize = clamp(Number(input.value), 16, 24);
    if (input.dataset.pref === 'lineHeight') prefs.lineHeight = clamp(Number(input.value), 1.65, 2.25);
    applyPrefs();
    savePrefs();
  });
  prefsPanel?.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : event.target?.parentElement;
    const button = target?.closest('button');
    if (!button || !prefsPanel.contains(button)) return;
    if (button.dataset.act === 'prefs-reset') Object.assign(prefs, DEFAULT_PREFS);
    else if (button.dataset.width) prefs.width = button.dataset.width;
    else if (button.dataset.theme) prefs.theme = button.dataset.theme;
    else if (button.dataset.font) prefs.font = button.dataset.font;
    else if (button.dataset.mode) prefs.mode = button.dataset.mode;
    else return;
    applyPrefs();
    savePrefs();
  });
  prefsPanel?.addEventListener('toggle', () => {
    document.querySelector('[data-act="prefs"]')?.setAttribute('aria-expanded', String(popovers.isOpen(prefsPanel)));
  });

  function scrollPercent() {
    const maximum = Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
    return clamp((window.scrollY / maximum) * 100, 0, 100);
  }

  function sectionLabelFor(element) {
    let heading = element;
    while (heading && !heading.classList?.contains('sec-h')) heading = heading.previousElementSibling;
    const label = (heading?.textContent || '').trim();
    return label.length > 18 ? label.slice(0, 18) + '...' : label;
  }

  function updateProgress(element = activeBlock) {
    const percent = scrollPercent();
    if (progressFill) progressFill.style.width = `${percent}%`;
    if (progressText) {
      const label = element ? sectionLabelFor(element) : '';
      progressText.textContent = `${Math.round(percent)}%${label ? ` · ${label}` : ''}`;
    }
  }

  function updateResumeState() {
    const button = document.querySelector('[data-act="resume"]');
    if (button) button.disabled = !readJson(progressKey, null)?.block_id;
  }

  function saveProgress(element = activeBlock) {
    if (!element?.dataset?.block) return;
    writeJson(progressKey, {
      block_id: element.dataset.block,
      section_id: element.dataset.section || '',
      scrollY: Math.round(window.scrollY),
      percent: Math.round(scrollPercent()),
      updated: Date.now(),
    });
    updateResumeState();
  }

  function queueProgressSave(element = activeBlock) {
    if (saveTimer) return;
    saveTimer = window.setTimeout(() => {
      saveTimer = 0;
      saveProgress(element);
    }, 700);
  }

  function activate(element) {
    if (!element) return;
    activeBlock = element;
    updateProgress(element);
    queueProgressSave(element);
  }

  function restoreProgress(animate) {
    const saved = readJson(progressKey, null);
    const element = saved?.block_id ? doc.querySelector(`[data-block="${CSS.escape(saved.block_id)}"]`) : null;
    if (!element) return false;
    activeBlock = element;
    element.scrollIntoView({ block: 'center', behavior: animate ? 'smooth' : 'auto' });
    updateProgress(element);
    if (animate) {
      element.classList.add('pulse');
      setTimeout(() => element.classList.remove('pulse'), 1500);
    }
    return true;
  }

  const observer = new IntersectionObserver((entries) => {
    const hit = entries.filter((entry) => entry.isIntersecting)
      .sort((a, b) => Math.abs(a.boundingClientRect.top - 120) - Math.abs(b.boundingClientRect.top - 120))[0];
    if (hit) activate(hit.target);
  }, { rootMargin: '-12% 0px -76% 0px', threshold: [0, 0.1, 0.5] });
  doc.querySelectorAll('.blk, .fig, .sub-h').forEach((element) => observer.observe(element));
  window.addEventListener('scroll', () => {
    if (progressFrame) return;
    progressFrame = requestAnimationFrame(() => {
      progressFrame = 0;
      updateProgress();
      queueProgressSave();
    });
  }, { passive: true });
  window.addEventListener('beforeunload', () => saveProgress());
  applyPrefs();
  updateResumeState();
  updateProgress();

  return {
    prefs,
    activate,
    isPrefsOpen: () => popovers.isOpen(prefsPanel),
    pageStep,
    restoreProgress,
    setFocusMode,
    setPrefsOpen,
  };
}
