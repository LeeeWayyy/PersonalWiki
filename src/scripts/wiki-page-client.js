import { t } from '../lib/i18n.mjs';

export function installIndexPageActions() {
  const grid = document.getElementById('idx-grid');
  const dialog = document.getElementById('idx-page-dialog');
  const select = document.getElementById('idx-merge-target');
  if (!grid || !(dialog instanceof HTMLDialogElement) || !(select instanceof HTMLSelectElement)) return;

  const title = dialog.querySelector('#idx-page-dialog-title');
  const sourceNodes = dialog.querySelectorAll('[data-op-source], [data-op-delete-source]');
  const targetNode = dialog.querySelector('[data-op-target]');
  const status = dialog.querySelector('.idx-op-status');
  const cancel = dialog.querySelector('[data-op-cancel]');
  const mergeConfirm = dialog.querySelector('[data-op-confirm="merge"]');
  const deleteConfirm = dialog.querySelector('[data-op-confirm="delete"]');
  const modeButtons = [...dialog.querySelectorAll('[data-op-mode]')];
  const panels = [...dialog.querySelectorAll('[data-op-panel]')];
  let card = null;
  let mode = 'merge';
  let busy = false;

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

  function open(button) {
    card = button.closest('.idx-card');
    if (!card) return;
    const name = card.dataset.t || card.dataset.rel;
    title.textContent = t('idx.managePage', { name });
    sourceNodes.forEach((node) => { node.textContent = name; });
    [...select.options].forEach((option) => {
      option.disabled = option.value === card.dataset.rel;
      if (option.value) option.textContent = `${option.dataset.title} · ${t(option.dataset.kindKey)}`;
    });
    select.value = '';
    setMode('merge');
    setBusy(false);
    syncTarget();
    dialog.showModal();
  }

  async function operate(action) {
    if (!card || busy) return;
    const rel = card.dataset.rel;
    const name = card.dataset.t || rel;
    const mergeInto = action === 'merge' ? select.value : null;
    if (action === 'merge' && !mergeInto) return;
    setBusy(true);
    status.classList.remove('error');
    status.textContent = t('idx.working');
    const backend = localStorage.getItem('backendUrl') || 'http://localhost:8787';
    const token = localStorage.getItem('backendToken') || '';
    try {
      const response = await fetch(`${backend}/wiki/page/remove`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(token ? { 'X-Auth-Token': token } : {}) },
        body: JSON.stringify({ rel, merge_into: mergeInto, confirmation: rel }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || response.statusText);
      status.textContent = t(action === 'merge' ? 'idx.merged' : 'idx.deleted', { name });
      if (payload.rebuilt) {
        location.reload();
        return;
      }
      card.remove();
      status.textContent += payload.rebuild_warning
        ? ` ${payload.rebuild_warning}`
        : t('idx.rebuildNeeded');
      select.disabled = true;
      modeButtons.forEach((button) => { button.disabled = true; });
      mergeConfirm.disabled = true;
      deleteConfirm.disabled = true;
      cancel.disabled = false;
    } catch (error) {
      status.classList.add('error');
      status.textContent = t('idx.opFailed', { detail: error instanceof Error ? error.message : String(error) });
      setBusy(false);
    }
  }

  grid.addEventListener('click', (event) => {
    const button = event.target instanceof Element ? event.target.closest('[data-page-manage]') : null;
    if (button) open(button);
  });
  modeButtons.forEach((button) => button.addEventListener('click', () => setMode(button.dataset.opMode)));
  select.addEventListener('change', syncTarget);
  mergeConfirm.addEventListener('click', () => operate('merge'));
  deleteConfirm.addEventListener('click', () => operate('delete'));
  dialog.addEventListener('cancel', (event) => { if (busy) event.preventDefault(); });
  dialog.addEventListener('close', () => { card = null; busy = false; });
}
