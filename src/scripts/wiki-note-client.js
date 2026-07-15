// Human-zone editor for wiki article pages. The ✎ Edit button and the
// "＋ add a personal note" bar both open a textarea over the page's
// human-zone; saving PUTs /wiki/human-zone (backend commits the markdown).
// Saved text is shown as pre-wrapped plain text until the next site build
// renders it as markdown.
import { t } from '../lib/i18n.mjs';
import { esc } from './list-filter.js';
import { api } from './backend-client.js';

export function installWikiNote() {
  const art = document.querySelector('article[data-wiki-rel]');
  if (!art) return;
  const rel = art.dataset.wikiRel;
  const prose = art.querySelector('.prose');
  let editor = null;

  function zoneEl(create) {
    let z = art.querySelector('.prose .zone-human');
    if (!z && create) {
      z = document.createElement('div');
      z.className = 'zone zone-human';
      art.querySelector('.prose').appendChild(z);
    }
    return z;
  }

  function showSaved(text) {
    const has = !!text.trim();
    art.classList.toggle('has-human', has);
    if (has) zoneEl(true).innerHTML = `<div style="white-space:pre-wrap">${esc(text)}</div>`;
    else zoneEl(false)?.replaceChildren();
  }

  async function openEditor() {
    if (editor) { editor.querySelector('textarea').focus(); return; }
    editor = document.createElement('div');
    editor.className = 'human-editor';
    editor.innerHTML = `<textarea rows="7" placeholder="${esc(t('wiki.humannote'))}"></textarea>
      <div class="row">
        <button type="button" class="btn-quiet save" data-act="save">${esc(t('act.save'))}</button>
        <button type="button" class="btn-quiet" data-act="cancel">${esc(t('act.cancel'))}</button>
        <span class="status"></span>
      </div>`;
    // The empty-state row is gone (the ＋ action-group button replaces it), so
    // the editor mounts just above the prose where the human-zone renders.
    prose.before(editor);
    art.classList.add('editing-human');
    const ta = editor.querySelector('textarea');
    const status = editor.querySelector('.status');
    const close = () => { editor.remove(); editor = null; art.classList.remove('editing-human'); };

    editor.querySelector('[data-act="cancel"]').addEventListener('click', close);
    editor.querySelector('[data-act="save"]').addEventListener('click', save);
    ta.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') save();
      if (e.key === 'Escape') close();
    });

    async function save() {
      status.textContent = t('rv.saving');
      try {
        const r = await api('/wiki/human-zone', { method: 'PUT', json: { rel, text: ta.value } });
        if (r.status === 401 || r.status === 403) { status.textContent = t('sr.tokenNeeded'); return; }
        if (!r.ok) { status.textContent = await r.text(); return; }
        showSaved(ta.value);
        close();
      } catch {
        status.textContent = t('sr.offlineNotes');
      }
    }

    // Textarea stays disabled until the current zone loads, so a failed load
    // can never be saved back as an empty note.
    ta.disabled = true;
    try {
      const r = await api(`/wiki/human-zone?rel=${encodeURIComponent(rel)}`);
      if (r.status === 401 || r.status === 403) { status.textContent = t('sr.tokenNeeded'); return; }
      if (!r.ok) { status.textContent = await r.text(); return; }
      ta.value = (await r.json()).text || '';
    } catch {
      status.textContent = t('sr.offlineNotes');
      return;
    }
    ta.disabled = false;
    ta.focus();
  }

  // Clicking a rendered note opens the editor; links inside notes keep working.
  art.addEventListener('click', (e) => {
    if (e.target.closest('a')) return;
    if (e.target.closest('.zone-human')) openEditor();
  });
  // Both the ✎ Edit and ＋ add-note buttons open the same human-zone editor.
  document.querySelector('[data-edit-page]')?.addEventListener('click', openEditor);
  document.querySelector('[data-add-note]')?.addEventListener('click', openEditor);
}
