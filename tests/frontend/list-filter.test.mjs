import assert from 'node:assert/strict';
import test from 'node:test';

import { esc } from '../../src/scripts/list-filter.js';

test('esc protects both HTML text and quoted attributes', () => {
  assert.equal(esc('" onfocus="x & <tag>'), '&quot; onfocus=&quot;x &amp; &lt;tag&gt;');
});
