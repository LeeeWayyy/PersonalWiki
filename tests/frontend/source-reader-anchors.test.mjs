import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  fuzzyLocateAnnotation,
  locateQuote,
  resolveDuplicateBlock,
} from '../../src/lib/source-reader-anchors.mjs';

const blocks = [
  { id: 'p-dup', section_id: 's-a', prev: '', next: 'p-mid', text: 'ATP stores energy.' },
  { id: 'p-mid', section_id: 's-a', prev: 'p-dup', next: 'p-dup', text: 'Bridge paragraph.' },
  { id: 'p-dup', section_id: 's-a', prev: 'p-mid', next: 'p-tail', text: 'ATP stores energy again.' },
  { id: 'p-tail', section_id: 's-a', prev: 'p-dup', next: '', text: 'Tail paragraph.' },
];

describe('source reader anchor helpers', () => {
  it('resolves duplicate block ids using neighbor context', () => {
    const target = {
      block_id: 'p-dup',
      section_id: 's-a',
      context: { prev_block_id: 'p-mid', next_block_id: 'p-tail' },
    };

    assert.equal(resolveDuplicateBlock(blocks, target), blocks[2]);
  });

  it('locates exact quotes by stored offset before falling back to search', () => {
    assert.deepEqual(
      locateQuote('abc abc abc', { quote: 'abc', start: 4 }),
      { start: 4, end: 7, exact: true },
    );
    assert.deepEqual(
      locateQuote('abc abc abc', { quote: 'abc' }),
      { start: 0, end: 3, exact: true },
    );
    assert.deepEqual(
      locateQuote('abc abc abc', { quote: 'abc', start: 40 }),
      { start: 8, end: 11, exact: true },
    );
  });

  it('uses stored prefix/suffix to disambiguate repeated quotes after offsets drift', () => {
    assert.deepEqual(
      locateQuote('ATP first. Bridge. ATP second.', {
        quote: 'ATP',
        start: 80,
        prefix: 'Bridge. ',
        suffix: ' second',
      }),
      { start: 19, end: 22, exact: true },
    );
  });

  it('fuzzy locates quotes across whitespace drift and small trailing edits', () => {
    const ws = fuzzyLocateAnnotation([{ id: 'p', text: 'ATP stores energy' }], {
      target: { block_id: 'p', selector: { quote: 'ATPstoresenergy' } },
    });
    assert.equal(ws.start, 0);
    assert.equal(ws.end, 17);

    const trailing = fuzzyLocateAnnotation([{ id: 'p', text: 'citric acid cycle' }], {
      target: { block_id: 'p', selector: { quote: 'citric acid cycle changed' } },
    });
    assert.equal(trailing.start, 0);
    assert.equal(trailing.end, 15);
  });

  it('searches the preferred block and nearby context before global fallback', () => {
    const hit = fuzzyLocateAnnotation(blocks, {
      target: {
        block_id: 'p-dup',
        context: { prev_block_id: 'p-mid', next_block_id: 'p-tail' },
        selector: { quote: 'ATP stores energy again with edit' },
      },
    });

    assert.equal(hit.block, blocks[2]);
    assert.equal(hit.start, 0);
  });
});
