// remark-inline — the vault's inline microsyntax:
//   [src:<id>#anchor, <id2>]   → citation chips linking to source pages
//   [[target]] / [[target|alias]] → wikilinks resolved via the alias index
//   ![[…assets/…]]              → image embeds (schema §14); page-transcludes are
//                                 downgraded to plain links (forbidden as embeds)
//   ==CONFLICT: …== / ==…==     → <mark> highlights (conflicts flagged)
import { visit } from 'unist-util-visit';
import { resolveSource, resolveWikilink, sourceReaderHref } from '../lib/vault.mjs';

const IMG_EXT = /\.(png|jpe?g|gif|svg|webp|avif)$/i;

function splitTextNodes(tree, regex, make) {
  visit(tree, 'text', (node, index, parent) => {
    if (!parent || index === null) return;
    const value = node.value;
    let last = 0, m;
    const out = [];
    regex.lastIndex = 0;
    while ((m = regex.exec(value))) {
      if (m.index > last) out.push({ type: 'text', value: value.slice(last, m.index) });
      const made = make(m);
      if (made) out.push(...made); else out.push({ type: 'text', value: m[0] });
      last = m.index + m[0].length;
    }
    if (!out.length) return;
    if (last < value.length) out.push({ type: 'text', value: value.slice(last) });
    parent.children.splice(index, 1, ...out);
    return index + out.length;
  });
}

function citationChip(id, anchor) {
  const src = resolveSource(id);
  const label = anchor || 'src';
  const cls = ['cite'];
  if (!src) cls.push('cite-unknown');
  // Deep-link into the source reader; the coarse chapter anchor (`#sec=<label>`)
  // resolves to that section's first block. Always target the reader (not gated on
  // build-time block existence — that isn't in Astro's render cache key, so it
  // would go stale); the reader shows an empty state if a source isn't extracted.
  const url = src ? sourceReaderHref(id, anchor || '') : '#';
  return {
    type: 'link', url,
    title: src ? src.title : `unknown source ${id}`,
    data: { hProperties: { className: cls, 'data-src': id } },
    children: [{ type: 'text', value: label }],
  };
}

function citationParts(value) {
  return String(value || '')
    .split(',')
    .map((part) => part.trim().replace(/^src:\s*/i, ''))
    .filter(Boolean)
    .map((part) => {
      const hash = part.indexOf('#');
      if (hash < 0) return { id: part.trim(), anchor: '' };
      return {
        id: part.slice(0, hash).trim(),
        anchor: part.slice(hash + 1).trim(),
      };
    })
    .filter((part) => part.id);
}

function vaultAssetUrl(target) {
  let path = target.trim().replace(/^\/+/, '');
  if (path.startsWith('sources/')) path = path.slice('sources/'.length);
  if (path.startsWith('lang/sources/')) path = 'lang/' + path.slice('lang/sources/'.length);
  return '/vault-assets/' + path.split('/').map(encodeURIComponent).join('/');
}

export default function remarkInline() {
  return (tree) => {
    // 1) Embeds ![[...]] — must run before wikilinks so we consume the `!`.
    splitTextNodes(tree, /!\[\[([^\]]+)\]\]/g, (m) => {
      const [target, alias] = m[1].split('|').map((s) => s.trim());
      if (IMG_EXT.test(target) || target.includes('.assets/')) {
        const url = vaultAssetUrl(target);
        return [{ type: 'image', url, alt: alias || '', data: { hProperties: { className: ['embed-img'], loading: 'lazy' } } }];
      }
      // Page transclude — not allowed as an embed; downgrade to a link.
      const href = resolveWikilink(target);
      return [{
        type: 'link', url: href || '#',
        data: { hProperties: { className: ['wikilink', href ? '' : 'wikilink-missing'].filter(Boolean) } },
        children: [{ type: 'text', value: alias || target }],
      }];
    });

    // 2) Wikilinks [[target|alias]]
    splitTextNodes(tree, /\[\[([^\]]+)\]\]/g, (m) => {
      const [target, alias] = m[1].split('|').map((s) => s.trim());
      const href = resolveWikilink(target);
      return [{
        type: 'link', url: href || `/search?q=${encodeURIComponent(target)}`,
        title: href ? '' : `unresolved: ${target}`,
        data: { hProperties: { className: ['wikilink', href ? '' : 'wikilink-missing'].filter(Boolean) } },
        children: [{ type: 'text', value: alias || target }],
      }];
    });

    // 3) Citations [src:...]
    splitTextNodes(tree, /\[src:([^\]]+)\]/g, (m) => {
      const nodes = [];
      citationParts(m[1]).forEach(({ id, anchor }, i) => {
        if (i > 0) nodes.push({ type: 'text', value: ' ' });
        nodes.push(citationChip(id, anchor));
      });
      return nodes;
    });

    // 4) Highlights ==...== (conflicts get an extra class)
    splitTextNodes(tree, /==([^=]+)==/g, (m) => {
      const text = m[1];
      const conflict = /^\s*CONFLICT/i.test(text);
      return [
        { type: 'html', value: `<mark class="hl${conflict ? ' hl-conflict' : ''}">` },
        { type: 'text', value: text },
        { type: 'html', value: '</mark>' },
      ];
    });
  };
}
