// remark-mermaid — turn ```mermaid fenced code into <pre class="mermaid"> so the
// client-side mermaid renderer (loaded on pages that need it) can draw it.
import { visit } from 'unist-util-visit';

function esc(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

export default function remarkMermaid() {
  return (tree) => {
    visit(tree, 'code', (node) => {
      if ((node.lang || '').toLowerCase() !== 'mermaid') return;
      node.type = 'html';
      node.value = `<pre class="mermaid">${esc(node.value)}</pre>`;
      delete node.lang;
      delete node.meta;
    });
  };
}
