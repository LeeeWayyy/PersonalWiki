// Swaps server-rendered (default-language) chrome to the saved language.
// Elements opt in via data-i18n (text), data-i18n-ph (placeholder),
// data-i18n-title (title attr); data-i18n-args holds JSON args for all three.
import { t, getLang, LANG_KEY } from '../lib/i18n.mjs';

function argsOf(el) {
  try { return el.dataset.i18nArgs ? JSON.parse(el.dataset.i18nArgs) : undefined; } catch { return undefined; }
}

export function applyI18n(root = document) {
  const lang = getLang();
  document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
  for (const el of root.querySelectorAll('[data-i18n]')) el.textContent = t(el.dataset.i18n, argsOf(el), lang);
  for (const el of root.querySelectorAll('[data-i18n-ph]')) el.placeholder = t(el.dataset.i18nPh, argsOf(el), lang);
  for (const el of root.querySelectorAll('[data-i18n-title]')) el.title = t(el.dataset.i18nTitle, argsOf(el), lang);
}

export function mountLangToggle() {
  document.querySelectorAll('[data-set-lang]').forEach((b) => {
    b.classList.toggle('on', b.dataset.setLang === getLang());
    b.addEventListener('click', () => {
      if (b.dataset.setLang === getLang()) return;
      localStorage.setItem(LANG_KEY, b.dataset.setLang);
      location.reload(); // ponytail: reload re-renders every dynamic string; no live re-render plumbing
    });
  });
}
