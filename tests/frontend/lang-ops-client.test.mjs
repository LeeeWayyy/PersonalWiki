import assert from 'node:assert/strict';
import test from 'node:test';

import { installLangOps } from '../../src/scripts/lang-ops-client.js';

const el = () => ({
  listeners: {},
  dataset: {},
  hidden: false,
  disabled: false,
  checked: false,
  textContent: '',
  addEventListener(type, listener) { this.listeners[type] = listener; },
  append(text) { this.textContent += text; },
});

test('language operations reset calibration and delete merged readers first', async (t) => {
  const card = (id, kind, title, checked) => {
    const input = { ...el(), checked };
    return { ...el(), dataset: { id, kind, title }, querySelector: () => input, input };
  };
  const cards = [
    card('book', 'book', 'Book', true),
    card('audio', 'audio', 'Audio', false),
    card('merged', 'merged', 'Merged', false),
  ];
  const ids = Object.fromEntries([
    'lang-actions', 'lang-merge-btn', 'lang-delete-btn', 'lang-sel-count',
    'lang-nocal-wrap', 'lang-nocal', 'lang-op-title', 'lang-op-hint',
  ].map((id) => [id, el()]));
  const log = el();
  const dialog = { ...el(), querySelector: () => log, showModal() {} };
  ids['lang-op-dialog'] = dialog;
  let reloads = 0;
  let rebuilt = true;
  let allFail = false;
  const requests = [];

  t.mock.method(globalThis, 'fetch', async (_path, options) => {
    const { source_id: sourceId } = JSON.parse(options.body);
    requests.push(sourceId);
    return allFail || sourceId === 'book'
      ? Response.json({ detail: 'still present' }, { status: 422 })
      : Response.json({ rebuilt });
  });
  globalThis.localStorage = { getItem: () => '' };
  globalThis.location = { reload: () => { reloads += 1; } };
  globalThis.confirm = () => true;
  globalThis.document = {
    querySelectorAll: () => cards,
    getElementById: (id) => ids[id],
  };
  t.after(() => {
    delete globalThis.document;
    delete globalThis.confirm;
    delete globalThis.location;
    delete globalThis.localStorage;
  });

  installLangOps();
  ids['lang-nocal'].checked = true;
  cards[1].input.checked = true;
  cards[1].input.listeners.change();
  assert.equal(ids['lang-nocal'].checked, false);

  cards[2].input.checked = true;
  await ids['lang-delete-btn'].listeners.click();
  assert.deepEqual(requests, ['merged', 'book', 'audio']);
  assert.equal(reloads, 1);
  assert.match(log.textContent, /2 removed · 1 failed · reloading/);

  rebuilt = false;
  await ids['lang-delete-btn'].listeners.click();
  assert.equal(reloads, 1);
  assert.match(log.textContent, /2 removed · 1 failed · rebuild the site to see the change/);

  allFail = true;
  await ids['lang-delete-btn'].listeners.click();
  assert.match(log.textContent, /0 removed · 3 failed —/);
  assert.doesNotMatch(log.textContent, /rebuild the site/);
});
