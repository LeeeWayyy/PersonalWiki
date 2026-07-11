// vault.mjs — read the synced ./vault tree and build indices the site + plugins
// share: pages, sources, an alias→href map for wikilinks, and a source→meta map
// for [src:] citations. Runs in Node at build time. No external deps.
import { readFileSync, readdirSync, statSync, existsSync } from 'node:fs';
import { join, relative, extname } from 'node:path';
import { createHash } from 'node:crypto';
import { fromMarkdown } from 'mdast-util-from-markdown';
import { gfm } from 'micromark-extension-gfm';
import { gfmFromMarkdown } from 'mdast-util-gfm';
import { toString as mdastToString } from 'mdast-util-to-string';
import { sourceReaderHrefForAnchor, sourceReaderSections } from './source-reader-chapters.mjs';
import { citationParts } from './source-citations.mjs';

const ROOT = process.cwd();
export const VAULT = join(ROOT, 'vault');
const WIKI = join(VAULT, 'wiki');

function walk(dir, out = []) {
  if (!existsSync(dir)) return out;
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) walk(p, out);
    else if (extname(p) === '.md') out.push(p);
  }
  return out;
}

function directMarkdownFiles(dir) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir)
    .map((name) => join(dir, name))
    .filter((p) => statSync(p).isFile() && extname(p) === '.md');
}

// Presentation helpers shared across pages (build-time).
// cleanTitle: strip the vault's date-prefix/extension noise from source titles.
export const cleanTitle = (t) => (t || '').replace(/^\d{4}-\d{2}-\d{2}-/, '').replace(/\.(epub|mobi|azw3?|pdf|md|txt)$/i, '').replace(/-/g, ' ').trim();
// enOf: first Latin-script alias, used as an English subtitle.
export const enOf = (p) => (p.aliases || []).find((a) => /[A-Za-z]/.test(a) && a !== p.title) || '';

function splitWikilinkTarget(target) {
  const raw = String(target || '').trim();
  const hash = raw.indexOf('#');
  if (hash < 0) return { base: raw, anchor: '' };
  return { base: raw.slice(0, hash).trim(), anchor: raw.slice(hash + 1).trim() };
}

function normalizeWikilinkKey(target) {
  const { base } = splitWikilinkTarget(target);
  return base
    .normalize('NFKC')
    .toLocaleLowerCase()
    // Match the common non-lowercase folds Python casefold() applies in
    // pipeline alias/lint resolution.
    .replace(/\u00df/g, 'ss')
    .replace(/\u03c2/g, '\u03c3')
    .replace(/\u017f/g, 's')
    .replace(/\s+/g, ' ')
    .trim();
}

// Tiny frontmatter parser — handles `key: value`, quoted strings, and inline
// flow arrays `[a, b, c]`. Sufficient for this vault's canonical frontmatter.
function parseFrontmatter(raw) {
  const m = raw.match(/^---\n([\s\S]*?)\n---\n?([\s\S]*)$/);
  if (!m) return { data: {}, body: raw };
  const data = {};
  for (const line of m[1].split('\n')) {
    const mm = line.match(/^([A-Za-z0-9_]+):\s*(.*)$/);
    if (!mm) continue;
    let [, k, v] = mm;
    v = v.trim();
    if (v.startsWith('[') && v.endsWith(']')) {
      data[k] = v.slice(1, -1).split(',').map((s) => s.trim().replace(/^['"]|['"]$/g, '')).filter(Boolean);
    } else if (v === '' || v === 'null' || v === '~') {
      data[k] = null;
    } else {
      data[k] = v.replace(/^['"]|['"]$/g, '');
    }
  }
  return { data, body: m[2] };
}

function h1Of(body) {
  const m = body.match(/^#\s+(.+)$/m);
  return m ? m[1].trim() : null;
}

let _cache = null;
export function loadVault() {
  if (_cache) return _cache;

  const pages = [];        // wiki pages (entities, topics, _index, _maps, taxonomy, index)
  const sources = [];      // source sidecars
  const aliasMap = new Map(); // alias/title/stem -> href
  const sourceMap = new Map(); // source_id -> meta

  // Wiki pages
  for (const file of walk(WIKI)) {
    const raw = readFileSync(file, 'utf8');
    const { data, body } = parseFrontmatter(raw);
    const rel = relative(WIKI, file).replace(/\.md$/, '');
    const slug = rel; // raw; Astro handles URL encoding of unicode segments
    const kind = rel.startsWith('_index/') ? 'index'
      : rel.startsWith('_maps/') ? 'map'
      : rel === '_taxonomy' ? 'taxonomy'
      : rel === 'index' ? 'home'
      : rel.startsWith('entities/') ? 'entity'
      : rel.startsWith('topics/') ? 'topic'
      : 'other';
    const title = h1Of(body) || data.title || rel.split('/').pop();
    const page = {
      rel, slug, kind, title, href: `/wiki/${slug}`,
      data, body,
      aliases: data.aliases || [],
      tags: data.tags || [],
      sources: data.sources || [],
      last_ingested: data.last_ingested || data.last_generated || null,
    };
    pages.push(page);

    if (kind === 'entity' || kind === 'topic') {
      const keys = new Set([rel.split('/').pop(), title, ...(data.aliases || [])]);
      for (const k of keys) {
        const key = normalizeWikilinkKey(k);
        if (key) aliasMap.set(key, page.href);
      }
    }
  }

  // Sources (wiki sidecars + lang sources). Only scan direct sidecars; .assets/
  // folders can contain Markdown manifests with the same source_id.
  for (const dir of [join(VAULT, 'sources'), join(VAULT, 'lang', 'sources')]) {
    for (const file of directMarkdownFiles(dir)) {
      const raw = readFileSync(file, 'utf8');
      const { data } = parseFrontmatter(raw);
      if (!data.source_id || sourceMap.has(data.source_id)) continue;
      const meta = {
        id: data.source_id,
        title: data.title || data.source_id,
        origin_type: data.origin_type || 'file',
        origin_ref: data.origin_ref || null,
        href: `/sources/${data.source_id}`,
        supersedes: data.supersedes || null,
      };
      sources.push(meta);
      sourceMap.set(data.source_id, meta);
    }
  }

  // Link graph: backlinks (incoming) + forward links (outgoing), by resolved href.
  const backlinks = new Map(); // href -> Set(href)  (who links TO this page)
  const forward = new Map();   // href -> Set(href)  (pages this page links to)
  const linkRe = /!?\[\[([^\]]+)\]\]/g;
  for (const page of pages) {
    let mm;
    linkRe.lastIndex = 0;
    while ((mm = linkRe.exec(page.body))) {
      if (mm[0].startsWith('!')) continue; // embed, not a link
      const target = mm[1].split('|')[0].trim();
      const href = aliasMap.get(normalizeWikilinkKey(target));
      if (href && href !== page.href) {
        if (!backlinks.has(href)) backlinks.set(href, new Set());
        backlinks.get(href).add(page.href);
        if (!forward.has(page.href)) forward.set(page.href, new Set());
        forward.get(page.href).add(href);
      }
    }
  }

  _cache = { pages, sources, aliasMap, sourceMap, backlinks, forward };
  return _cache;
}

// --- Language module -------------------------------------------------------
// Current reader source: pipeline-emitted `_reading/<slug>.reading.json`.
const LANG = join(VAULT, 'lang');

let _lang = null;
const paragraphsOfReadingChapter = (chapter = {}) => chapter.paragraphs || [{ sentences: chapter.sentences || [] }];
const sentenceText = (sentence = {}) => sentence.jp || sentence.text || (sentence.tokens || []).map((token) => token.t || '').join('');

export function readingStats(reading) {
  const stats = {
    word_count: null,
    token_count: null,
    grammar_count: null,
    chapter_count: reading?.chapters?.length || 0,
  };
  if (!reading) return stats;

  let sawTokens = false;
  let words = 0;
  let tokens = 0;
  let chapterGrammar = 0;
  let sentenceGrammar = 0;

  for (const chapter of reading.chapters || []) {
    if (Array.isArray(chapter.grammar)) chapterGrammar += chapter.grammar.length;
    for (const paragraph of paragraphsOfReadingChapter(chapter)) {
      for (const sentence of paragraph.sentences || []) {
        if (Array.isArray(sentence.tokens)) {
          sawTokens = true;
          tokens += sentence.tokens.length;
          words += sentence.tokens.filter((token) => token?.w).length;
        }
        if (Array.isArray(sentence.grammar)) sentenceGrammar += sentence.grammar.length;
      }
    }
  }

  if (sawTokens) {
    stats.word_count = words;
    stats.token_count = tokens;
  }
  const grammar = chapterGrammar || sentenceGrammar;
  if (grammar) stats.grammar_count = grammar;
  return stats;
}

export function loadLang() {
  if (_lang) return _lang;
  const out = [];
  const rdir = join(LANG, '_reading');

  if (existsSync(rdir)) {
    for (const rf of readdirSync(rdir)) {
      if (!rf.endsWith('.reading.json')) continue;
      try {
        const doc = JSON.parse(readFileSync(join(rdir, rf), 'utf8'));
        const slug = rf.replace(/\.reading\.json$/, '');
        const id = doc.source_id || slug;
        if (!id) continue;
        const stats = readingStats(doc);
        out.push({
          id,
          slug,
          title: doc.title || slug,
          vocab: [],
          grammar: [],
          ...stats,
          chapters: (doc.chapters || []).map((ch) => ({
            heading: ch.chapter || ch.heading || '',
            text: paragraphsOfReadingChapter(ch)
              .map((p) => (p.sentences || []).map(sentenceText).join(''))
              .filter(Boolean)
              .join('\n\n'),
          })),
          reading: doc,
        });
      } catch { /* ignore malformed */ }
    }
  }
  _lang = out;
  return out;
}

// --- Source Reader blocks (contract documented in pipeline/schema.md) --------
// Block identity hashes ONLY type + normalized text (nothing positional/section-
// scoped). section_id is non-positional (hash of the heading-text). Canonical
// block.text is the paragraph's sentence `jp` joined with no separator.
const _sha8 = (s) => createHash('sha256').update(s).digest('hex').slice(0, 8);
const _normHash = (t) => (t || '').replace(/\uFEFF/g, '').replace(/\s+/g, '').trim();

export function sourceReaderBlockId(type, text) {
  const prefix = type === 'heading' ? 'h-' : 'p-';
  return prefix + _sha8(`${type}\x1f${_normHash(text)}`);
}

export function sourceReaderSectionId(section) {
  return 's-' + _sha8(`SEC\x1f${_normHash(section)}`);
}

export function blocksForSource(sourceId) {
  // Prefer an extracted blocks artifact (documents: epub/pdf via build-blocks.py).
  const bp = join(VAULT, '.blocks', `${sourceId}.blocks.json`);
  if (existsSync(bp)) {
    try {
      const doc = JSON.parse(readFileSync(bp, 'utf8'));
      const b = doc.blocks || [];
      for (let i = 0; i < b.length; i++) {
        if (b[i].prev === undefined) b[i].prev = i > 0 ? b[i - 1].id : '';
        if (b[i].next === undefined) b[i].next = i < b.length - 1 ? b[i + 1].id : '';
      }
      return { source_id: sourceId, title: doc.title || sourceId, lang: doc.lang || '', blocks: b };
    } catch { /* fall through to reading-derived */ }
  }
  // Fallback: derive from the source's reading.json (lang sources).
  const entry = loadLang().find((e) => e.id === sourceId && e.reading);
  if (!entry) return null; // no readable text yet
  const doc = entry.reading;
  const blocks = [];
  let order = 0;
  for (const ch of doc.chapters) {
    const section = ch.chapter || '';
    const section_id = sourceReaderSectionId(section);
    const paras = paragraphsOfReadingChapter(ch);
    for (const p of paras) {
      const text = (p.sentences || []).map(sentenceText).join(''); // canonical, frozen
      if (!text) continue;
      const id = sourceReaderBlockId('paragraph', text);
      blocks.push({ id, type: 'paragraph', section_id, section, order: order++, text });
    }
  }
  for (let i = 0; i < blocks.length; i++) {
    blocks[i].prev = i > 0 ? blocks[i - 1].id : '';
    blocks[i].next = i < blocks.length - 1 ? blocks[i + 1].id : '';
  }
  return { source_id: sourceId, title: entry.title, lang: doc.lang || 'ja', blocks };
}

export function sourceHasBlocks(sourceId) {
  const b = blocksForSource(sourceId);
  return !!(b && b.blocks.length);
}

export function chaptersForSource(sourceId) {
  const doc = blocksForSource(sourceId);
  return sourceReaderSections(doc?.blocks || []);
}

export function sourceReaderHref(sourceId, anchor = '') {
  const src = resolveSource(sourceId);
  if (!src) return '#';
  return sourceReaderHrefForAnchor(sourceId, chaptersForSource(sourceId), anchor);
}

// --- AI-citation index (contract documented in pipeline/schema.md) -----------
// Walk each wiki page's mdast to find every `[src:<id>#anchor]` and the text of
// its ENCLOSING block node (paragraph / heading / list item / blockquote callout
// / table cell — citations appear in all of these, so we key off the nearest
// block ancestor, not "paragraph"). This is the bidirectional half of linking:
// from a source block, see which wiki claims cite it. Built once, cached.
const _BLOCK_TYPES = new Set(['paragraph', 'heading', 'listItem', 'blockquote', 'tableCell']);
const _CITE_RE = /\[src:([^\]]+)\]/g;

function _cleanExcerpt(s, max = 150) {
  let t = (s || '')
    .replace(/^\s*\[!\w+\]\s*/, '')                                   // callout marker [!AI]
    .replace(/\[src:[^\]]*\]/g, '')                                   // citation tokens
    .replace(/!?\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (_m, a, b) => b || a) // wikilink → label
    .replace(/==+/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  const chars = [...t];
  if (chars.length > max) t = chars.slice(0, max).join('').trim() + '…';
  return t;
}

function _collectCitations(node, block, out) {
  const cur = _BLOCK_TYPES.has(node.type) ? node : block;
  if (node.type === 'text' && node.value.includes('[src:')) {
    _CITE_RE.lastIndex = 0;
    let m;
    while ((m = _CITE_RE.exec(node.value))) {
      for (const { id, anchor } of citationParts(m[1])) {
        out.push({ source_id: id, anchor: anchor || '', block: cur });
      }
    }
  }
  if (node.children) for (const c of node.children) _collectCitations(c, cur, out);
}

let _citations = null;
function citationIndex() {
  if (_citations) return _citations;
  const { pages } = loadVault();
  const map = new Map(); // source_id -> [{ wiki_href, wiki_title, anchor, excerpt, kind }]
  for (const page of pages) {
    let tree;
    try {
      tree = fromMarkdown(page.body, { extensions: [gfm()], mdastExtensions: [gfmFromMarkdown()] });
    } catch { continue; }
    const raw = [];
    _collectCitations(tree, null, raw);
    for (const c of raw) {
      const full = c.block ? mdastToString(c.block) : '';
      const ai = /^\s*\[!\w+\]/.test(full);
      const entry = {
        wiki_href: page.href, wiki_title: page.title,
        anchor: c.anchor, excerpt: _cleanExcerpt(full),
        kind: ai ? 'ai' : (c.block ? c.block.type : 'text'),
      };
      if (!map.has(c.source_id)) map.set(c.source_id, []);
      const arr = map.get(c.source_id);
      const key = entry.wiki_href + '\x1f' + entry.anchor + '\x1f' + entry.excerpt;
      if (!arr.some((e) => (e.wiki_href + '\x1f' + e.anchor + '\x1f' + e.excerpt) === key)) arr.push(entry);
    }
  }
  _citations = map;
  return map;
}

export function citationsForSource(sourceId) {
  return citationIndex().get(String(sourceId).trim()) || [];
}

export function resolveWikilink(target) {
  const { aliasMap } = loadVault();
  const { anchor } = splitWikilinkTarget(target);
  const href = aliasMap.get(normalizeWikilinkKey(target));
  return href && anchor ? `${href}#${encodeURIComponent(anchor)}` : href || null;
}
export function resolveSource(id) {
  const { sourceMap } = loadVault();
  return sourceMap.get(String(id).trim()) || null;
}

// Direct-neighbor graph for a page: outgoing links + backlinks, merged.
export function pageNeighbors(href) {
  const { forward, backlinks, pages } = loadVault();
  const out = forward.get(href) || new Set();
  const inc = backlinks.get(href) || new Set();
  const titleOf = (h) => (pages.find((p) => p.href === h) || {}).title || h;
  const seen = new Map(); // href -> dir
  for (const h of out) seen.set(h, 'out');
  for (const h of inc) seen.set(h, seen.has(h) ? 'both' : 'in');
  return [...seen.entries()].map(([h, dir]) => ({ href: h, title: titleOf(h), dir }));
}
