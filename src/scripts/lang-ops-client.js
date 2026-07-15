import { api, streamJob } from './backend-client.js';

// Reader-index selection → merge a book reader + an audio reader into a new one.
// Merge is a long backend job; we stream its log into a dialog, then reload.
// ponytail: hardcoded English strings + delete deferred to Phase 2.
export function installLangOps() {
  const cards = [...document.querySelectorAll('.reader-card')];
  const bar = document.getElementById('lang-actions');
  const mergeBtn = document.getElementById('lang-merge-btn');
  const count = document.getElementById('lang-sel-count');
  const dlg = document.getElementById('lang-merge-dialog');
  const log = dlg?.querySelector('.log');
  if (!cards.length || !bar || !mergeBtn || !count || !dlg || !log) return;

  const picked = () => cards.filter((c) => c.querySelector('input')?.checked);
  const ofKind = (list, k) => list.filter((c) => c.dataset.kind === k);

  function sync() {
    const sel = picked();
    bar.hidden = sel.length === 0;
    count.textContent = String(sel.length);
    const ok = sel.length === 2 && ofKind(sel, 'book').length === 1 && ofKind(sel, 'audio').length === 1;
    mergeBtn.disabled = !ok;
    mergeBtn.title = ok ? 'Merge into a new reader'
      : 'Select exactly one book reader and one audio reader';
  }

  cards.forEach((c) => c.querySelector('input')?.addEventListener('change', sync));

  mergeBtn.addEventListener('click', async () => {
    const sel = picked();
    const book = ofKind(sel, 'book')[0];
    const audio = ofKind(sel, 'audio')[0];
    if (!book || !audio) return;
    log.textContent = '';
    const append = (line) => { log.textContent += line + '\n'; log.scrollTop = log.scrollHeight; };
    dlg.showModal();
    append(`Merging "${book.dataset.title}" + "${audio.dataset.title}" …`);
    try {
      const res = await api('/lang/merge', {
        method: 'POST',
        json: { book_id: book.dataset.id, audio_id: audio.dataset.id },
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(payload.detail || res.statusText);
      await streamJob(payload.job_id, append);
      append('— done · reloading —');
      location.reload();
    } catch (error) {
      append('error: ' + (error instanceof Error ? error.message : String(error)));
    }
  });

  sync();
}
