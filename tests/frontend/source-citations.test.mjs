import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

import {
  citationParts,
  decodeSourceAnchor,
  encodeSectionAnchor,
  sourceCitationRef,
} from '../../src/lib/source-citations.mjs';

const cases = JSON.parse(readFileSync(
  new URL('../../ci-fixtures/source-citation-contract.json', import.meta.url),
  'utf8',
));

describe('source citation contract', () => {
  it('matches the cross-runtime encoding fixture', () => {
    for (const testCase of cases) {
      assert.equal(encodeSectionAnchor(testCase.label), testCase.anchor);
      assert.equal(decodeSourceAnchor(testCase.anchor), testCase.label);
    }
  });

  it('round-trips delimiter characters as one part in a citation group', () => {
    const label = cases[1].label;
    const parts = citationParts(
      `${sourceCitationRef('SOURCE1', label)},src:SOURCE2#legacy section`,
    );
    assert.deepEqual(parts, [
      { id: 'SOURCE1', anchor: label },
      { id: 'SOURCE2', anchor: 'legacy section' },
    ]);
  });

  it('leaves structured and malformed legacy anchors untouched', () => {
    assert.equal(decodeSourceAnchor('card-2'), 'card-2');
    assert.equal(decodeSourceAnchor('sec=bad%ZZvalue'), 'bad%ZZvalue');
  });
});
