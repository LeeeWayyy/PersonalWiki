import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import remarkInline from '../../src/plugins/remark-inline.mjs';

const SOURCE_ID = 'S1FIXTURE000000000000000000';
const MD_SOURCE_ID = 'S1MDFIXTURE000000000000000';

function renderTextNode(value) {
  const tree = {
    type: 'root',
    children: [
      {
        type: 'paragraph',
        children: [{ type: 'text', value }],
      },
    ],
  };
  remarkInline()(tree);
  return tree.children[0].children;
}

describe('remark-inline citation rendering', () => {
  it('renders multi-source citations that repeat the src prefix', () => {
    const nodes = renderTextNode(`[src:${SOURCE_ID}#第一章,src:${MD_SOURCE_ID}#第二章]`);
    const links = nodes.filter((node) => node.type === 'link');

    assert.equal(links.length, 2);
    assert.equal(links[0].data.hProperties['data-src'], SOURCE_ID);
    assert.equal(links[0].children[0].value, '第一章');
    assert.equal(links[1].data.hProperties['data-src'], MD_SOURCE_ID);
    assert.equal(links[1].children[0].value, '第二章');
    assert.equal(links[1].url.includes('src:'), false);
  });
});
