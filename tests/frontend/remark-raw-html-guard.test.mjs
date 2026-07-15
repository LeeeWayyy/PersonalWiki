import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import remarkRawHtmlGuard from '../../src/plugins/remark-raw-html-guard.mjs';

function check(value) {
  const tree = { type: 'root', children: [{ type: 'html', value }] };
  remarkRawHtmlGuard()(tree, {
    fail(message) { throw new Error(message); },
  });
}

describe('remark raw HTML guard', () => {
  it('allows zone markers', () => {
    assert.doesNotThrow(() => check('<!-- llm-zone -->'));
    assert.doesNotThrow(() => check('<!-- /human-zone -->'));
  });

  it('rejects executable HTML wrapped in comments', () => {
    assert.throws(
      () => check('<!-- ok --><script>globalThis.PW_XSS=1</script><!-- x -->'),
      /Raw HTML is not allowed/,
    );
  });
});
