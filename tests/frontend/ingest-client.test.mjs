import assert from 'node:assert/strict';
import test from 'node:test';

import { ingestOptions, isSectionableFile } from '../../src/scripts/ingest-client.js';

test('ingest options leave auto-chaptering untouched without a section heading', () => {
  assert.deepEqual(ingestOptions('auto', '  '), {
    kind: 'auto',
    section_heading: null,
  });
});

test('ingest options send a selector rather than a label-only override', () => {
  assert.deepEqual(ingestOptions('wiki', '  第二章 生命力  '), {
    kind: 'wiki',
    section_heading: '第二章 生命力',
  });
});

test('ingest options omit section selectors for unsupported kinds', () => {
  for (const kind of ['video', 'audio', 'image_note', 'lang']) {
    assert.deepEqual(ingestOptions(kind, '第二章 生命力'), {
      kind,
      section_heading: null,
    });
  }
});

test('sectionable file detection stays with the file-input handler', () => {
  for (const name of ['book.epub', 'paper.PDF', 'notes.markdown', 'page.html']) {
    assert.equal(isSectionableFile(name), true);
  }
  assert.equal(isSectionableFile('cover.png'), false);
});
