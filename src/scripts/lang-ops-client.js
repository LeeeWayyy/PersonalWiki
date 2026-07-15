import { api, streamJob } from './backend-client.js';

// Reader-index selection → merge (book + audio → new reader) or delete readers.
// Merge is a long streamed job; delete is a fast synchronous call per reader.
// ponytail: hardcoded English strings; native confirm() gates delete.
export function installLangOps() {
  const cards = [...document.querySelectorAll('.reader-card')];
  const bar = document.getElementById('lang-actions');
  const mergeBtn = document.getElementById('lang-merge-btn');
  const deleteBtn = document.getElementById('lang-delete-btn');
  const count = document.getElementById('lang-sel-count');
  const nocalWrap = document.getElementById('lang-nocal-wrap');
  const nocal = document.getElementById('lang-nocal');
  const dlg = document.getElementById('lang-op-dialog');
  const title = document.getElementById('lang-op-title');
  const hint = document.getElementById('lang-op-hint');
  const log = dlg?.querySelector('.log');
  if (!cards.length || !bar || !mergeBtn || !deleteBtn || !count || !dlg || !log) return;

  let busy = false;
  const picked = () => cards.filter((c) => c.querySelector('input')?.checked);
  const ofKind = (list, k) => list.filter((c) => c.dataset.kind === k);

  function sync() {
    const sel = picked();
    bar.hidden = sel.length === 0;
    count.textContent = String(sel.length);
    const mergeable = sel.length === 2 && ofKind(sel, 'book').length === 1 && ofKind(sel, 'audio').length === 1;
    mergeBtn.disabled = busy || !mergeable;
    mergeBtn.title = mergeable ? 'Merge into a new reader'
      : 'Select exactly one book reader and one audio reader';
    if (nocalWrap) nocalWrap.hidden = !mergeable;
    deleteBtn.disabled = busy || sel.length === 0;
  }

  function startDialog(heading, withHint) {
    title.textContent = heading;
    hint.hidden = !withHint;
    log.textContent = '';
    dlg.showModal();
    return (line) => { log.append(line + '\n'); log.scrollTop = log.scrollHeight; };
  }

  cards.forEach((c) => c.querySelector('input')?.addEventListener('change', () => {
    if (nocal) nocal.checked = false;
    sync();
  }));

  mergeBtn.addEventListener('click', async () => {
    if (busy) return;
    const sel = picked();
    const book = ofKind(sel, 'book')[0];
    const audio = ofKind(sel, 'audio')[0];
    if (!book || !audio) return;
    busy = true;
    sync();
    const calibrate = !nocal?.checked;
    const append = startDialog('Merging readers', true);
    append(`Merging "${book.dataset.title}" + "${audio.dataset.title}" …`
      + (calibrate ? '' : ' (timing only, no re-calibration)'));
    try {
      const res = await api('/lang/merge', {
        method: 'POST',
        json: { book_id: book.dataset.id, audio_id: audio.dataset.id, calibrate },
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(payload.detail || res.statusText);
      await streamJob(payload.job_id, append);
      append('— done · reloading —');
      location.reload();
    } catch (error) {
      append('error: ' + (error instanceof Error ? error.message : String(error)));
    } finally {
      busy = false;
      sync();
    }
  });

  deleteBtn.addEventListener('click', async () => {
    if (busy) return;
    const sel = picked().sort((a, b) => Number(b.dataset.kind === 'merged') - Number(a.dataset.kind === 'merged'));
    if (!sel.length) return;
    const names = sel.map((c) => c.dataset.title).join(', ');
    if (!confirm(`Delete ${sel.length} reader(s)?\n\n${names}\n\nThis removes their pages and cannot be undone.`)) return;
    busy = true;
    sync();
    const append = startDialog('Deleting readers', false);
    let latestRebuilt = false;
    let removed = 0;
    const failed = [];
    // ponytail: one reader per call — bulk fans out sequentially.
    for (const c of sel) {
      append(`Deleting "${c.dataset.title}" …`);
      try {
        const res = await api('/lang/source/remove', {
          method: 'POST',
          json: { source_id: c.dataset.id, confirmation: c.dataset.id },
        });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(payload.detail || res.statusText);
        latestRebuilt = !!payload.rebuilt;
        removed += 1;
        if (payload.rebuild_warning) append(`  ⚠ rebuild: ${payload.rebuild_warning}`);
        append('  removed ✓');
      } catch (error) {
        failed.push(c.dataset.title);
        append('  error: ' + (error instanceof Error ? error.message : String(error)));
      }
    }
    busy = false;
    sync();
    if (failed.length) {
      const next = latestRebuilt ? ' · reloading' : removed ? ' · rebuild the site to see the change' : '';
      append(`— ${removed} removed · ${failed.length} failed${next} —`);
      if (latestRebuilt) location.reload();
      return;
    }
    if (latestRebuilt) { append('— done · reloading —'); location.reload(); }
    else append('— done · rebuild the site to see the change —');
  });

  sync();
}
