import assert from 'node:assert/strict';
import test from 'node:test';

import { charRange } from '../../src/scripts/source-reader-marks.js';

test('charRange maps document offsets across text nodes', () => {
  const nodes = [{ nodeValue: 'abc' }, { nodeValue: 'def' }];
  const range = {
    setStart(node, offset) { this.startContainer = node; this.startOffset = offset; },
    setEnd(node, offset) { this.endContainer = node; this.endOffset = offset; },
  };
  const previousDocument = globalThis.document;
  const previousNodeFilter = globalThis.NodeFilter;
  globalThis.NodeFilter = { SHOW_TEXT: 4 };
  globalThis.document = {
    createRange: () => range,
    createTreeWalker: () => {
      let index = 0;
      return { nextNode: () => nodes[index++] || null };
    },
  };
  try {
    assert.equal(charRange({}, 2, 5), range);
    assert.deepEqual(
      [range.startContainer, range.startOffset, range.endContainer, range.endOffset],
      [nodes[0], 2, nodes[1], 2],
    );
    assert.equal(charRange({}, 0, 9), null);
  } finally {
    globalThis.document = previousDocument;
    globalThis.NodeFilter = previousNodeFilter;
  }
});
