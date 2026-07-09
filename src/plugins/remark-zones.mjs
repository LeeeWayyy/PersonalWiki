// remark-zones — turn the vault's HTML-comment zone markers into styled
// containers, and turn `> [!AI] …` Obsidian callouts into styled panels.
//
//   <!-- llm-zone -->  …  <!-- /llm-zone -->   →  <div class="zone zone-llm"> … </div>
//   <!-- human-zone --> … <!-- /human-zone -->  →  <div class="zone zone-human"> … </div>
//   (also moc-zone, map-zone, vocab-zone, grammar-zone, study-zone, reading-zone)
//
// The two-tier `### Synthesis` / `### From src:<id>` structure (schema §3) needs
// no special AST work — those are ordinary headings; we style them via CSS on
// `.zone-llm h3`. This plugin just establishes the zone boundary + callout.
import { visit } from 'unist-util-visit';

const ZONE_RE = /^<!--\s*(\/?)([a-z][a-z-]*)-zone\s*-->$/;

export default function remarkZones() {
  return (tree) => {
    // Rewrite zone-marker HTML comments into raw <div> open/close nodes.
    visit(tree, 'html', (node) => {
      const m = node.value.trim().match(ZONE_RE);
      if (!m) return;
      const [, close, name] = m;
      node.value = close ? '</div>' : `<div class="zone zone-${name}">`;
    });

    // Obsidian callouts: a blockquote whose first line is `[!TYPE] Title`.
    visit(tree, 'blockquote', (node) => {
      const first = node.children[0];
      if (!first || first.type !== 'paragraph') return;
      const t = first.children[0];
      if (!t || t.type !== 'text') return;
      const m = t.value.match(/^\[!([A-Za-z]+)\]\s*(.*)$/);
      if (!m) return;
      const type = m[1].toLowerCase();
      const title = m[2].trim();
      node.data = node.data || {};
      node.data.hProperties = { className: ['callout', `callout-${type}`], 'data-callout': type };
      // Replace the marker line with a styled title element.
      const rest = first.children.slice(1);
      // Drop a leading hard-break/newline left by removing the marker.
      if (rest[0] && rest[0].type === 'break') rest.shift();
      const titleNode = {
        type: 'paragraph',
        data: { hProperties: { className: ['callout-title'] } },
        children: [{ type: 'text', value: title || type.toUpperCase() }],
      };
      first.children = rest;
      if (first.children.length === 0) node.children.shift();
      node.children.unshift(titleNode);
    });
  };
}
