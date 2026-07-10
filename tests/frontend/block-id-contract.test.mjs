import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, it } from 'node:test';

import { sourceReaderBlockId } from '../../src/lib/vault.mjs';

const fixture = JSON.parse(readFileSync(new URL('../../ci-fixtures/block-id-contract.json', import.meta.url), 'utf8'));

describe('source reader block id contract', () => {
  it('matches the shared compatibility fixture', () => {
    for (const block of fixture.blocks) {
      assert.equal(sourceReaderBlockId(block.type, block.text), block.expected_id, `${block.type}: ${block.text}`);
    }
  });
});
