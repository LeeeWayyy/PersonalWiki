import { t } from '../lib/i18n.mjs';
import { api } from './backend-client.js';

// Drives the shared #idx-page-dialog for merge/delete over one page (wiki
// article action group) or many (index selection mode). Returns { open }
// where open(items, onDone) takes items = [{ rel, name, card? }].
export function installPageOps() {
  const dialog = document.getElementById('idx-page-dialog');
  const select = document.getElementById('idx-merge-target');
  if (!(dialog instanceof HTMLDialogElement) || !(select instanceof HTMLSelectElement)) return null;

  const title = dialog.querySelector('#idx-page-dialog-title');
  const sourceNodes = dialog.querySelectorAll('[data-op-source], [data-op-delete-source]');
  const targetNode = dialog.querySelector('[data-op-target]');
  const status = dialog.querySelector('.idx-op-status');
  const cancel = dialog.querySelector('[data-op-cancel]');
  const mergeConfirm = dialog.querySelector('[data-op-confirm="merge"]');
  const deleteConfirm = dialog.querySelector('[data-op-confirm="delete"]');
  const modeButtons = [...dialog.querySelectorAll('[data-op-mode]')];
  const panels = [...dialog.querySelectorAll('[data-op-panel]')];
  let items = [];
  let onDone = null;
  let mode = 'merge';
  let busy = false;

  const label = () => (items.length === 1 ? items[0].name : t('idx.nPages', { n: items.length }));

  function setMode(next) {
    mode = next;
    modeButtons.forEach((button) => button.classList.toggle('on', button.dataset.opMode === mode));
    panels.forEach((panel) => { panel.hidden = panel.dataset.opPanel !== mode; });
    mergeConfirm.hidden = mode !== 'merge';
    deleteConfirm.hidden = mode !== 'delete';
    status.textContent = '';
  }

  function syncTarget() {
    const option = select.selectedOptions[0];
    targetNode.textContent = option?.value ? option.dataset.title : t('idx.chooseTarget');
    mergeConfirm.disabled = busy || !option?.value;
  }

  function setBusy(next) {
    busy = next;
    select.disabled = next;
    cancel.disabled = next;
    modeButtons.forEach((button) => { button.disabled = next; });
    deleteConfirm.disabled = next;
    syncTarget();
  }

  function open(next, done, startMode) {
    items = next.filter(Boolean);
    if (!items.length) return;
    onDone = done || null;
    title.textContent = items.length === 1
      ? t('idx.managePage', { name: items[0].name })
      : t('idx.manageN', { n: items.length });
    sourceNodes.forEach((node) => { node.textContent = label(); });
    const rels = new Set(items.map((i) => i.rel));
    [...select.options].forEach((option) => {
      option.disabled = rels.has(option.value);
      if (option.value) option.textContent = `${option.dataset.title} · ${t(option.dataset.kindKey)}`;
    });
    select.value = '';
    setMode(startMode === 'delete' ? 'delete' : 'merge');
    setBusy(false);
    syncTarget();
    dialog.showModal();
  }

  async function operate(action) {
    if (!items.length || busy) return;
    const mergeInto = action === 'merge' ? select.value : null;
    if (action === 'merge' && !mergeInto) return;
    setBusy(true);
    status.classList.remove('error');
    status.textContent = t('idx.working');
    let rebuilt = false;
    const failed = [];
    // ponytail: the backend removes one page per call — bulk fans out sequentially.
    for (const it of items) {
      try {
        const response = await api('/wiki/page/remove', {
          method: 'POST',
          json: { rel: it.rel, merge_into: mergeInto, confirmation: it.rel },
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || response.statusText);
        rebuilt = rebuilt || !!payload.rebuilt;
        it.card?.remove();
      } catch (error) {
        failed.push(`${it.name}: ${error instanceof Error ? error.message : String(error)}`);
      }
    }
    if (failed.length) {
      status.classList.add('error');
      status.textContent = t('idx.opFailed', { detail: failed.join(' · ') });
      setBusy(false);
      return;
    }
    if (rebuilt) { location.reload(); return; }
    status.textContent = t(action === 'merge' ? 'idx.merged' : 'idx.deleted', { name: label() }) + t('idx.rebuildNeeded');
    onDone?.();
    select.disabled = true;
    modeButtons.forEach((button) => { button.disabled = true; });
    mergeConfirm.disabled = true;
    deleteConfirm.disabled = true;
    cancel.disabled = false;
  }

  modeButtons.forEach((button) => button.addEventListener('click', () => setMode(button.dataset.opMode)));
  select.addEventListener('change', syncTarget);
  mergeConfirm.addEventListener('click', () => operate('merge'));
  deleteConfirm.addEventListener('click', () => operate('delete'));
  dialog.addEventListener('cancel', (event) => { if (busy) event.preventDefault(); });
  dialog.addEventListener('close', () => { items = []; onDone = null; busy = false; });

  return { open };
}
