import { chapterForSourceAnchor } from '../lib/source-reader-chapters.mjs';

function readChapters(el) {
  try {
    return JSON.parse(el.dataset.chapters || '[]') || [];
  } catch {
    return [];
  }
}

export function bootSourceReaderRedirect() {
  const el = document.getElementById('sr-read-redirect');
  if (!el) return;
  const fallbackHref = el.dataset.href || '';
  const chapter = chapterForSourceAnchor(readChapters(el), window.location.hash);
  const href = chapter?.href || fallbackHref;
  if (href) window.location.replace(href + window.location.hash);
}
