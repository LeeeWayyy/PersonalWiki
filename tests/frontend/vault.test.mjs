import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  blocksForSource,
  cleanTitle,
  citationsForSource,
  loadLang,
  loadVault,
  resolveWikilink,
  sourceReaderHref,
  sourceHasBlocks,
} from '../../src/lib/vault.mjs';

const SOURCE_ID = 'S1FIXTURE000000000000000000';
const MD_SOURCE_ID = 'S1MDFIXTURE000000000000000';

describe('vault frontend contracts', () => {
  it('indexes fixture pages, sources, aliases, and backlinks', () => {
    const vault = loadVault();
    assert.equal(vault.sources.some((source) => source.id === SOURCE_ID), true);
    assert.equal(vault.sources.some((source) => source.id === MD_SOURCE_ID), true);
    assert.equal(resolveWikilink('ATP'), '/wiki/entities/ATP');
    assert.equal(resolveWikilink('Adenosine triphosphate'), '/wiki/entities/ATP');
    assert.equal(resolveWikilink('  adenosine   triphosphate  '), '/wiki/entities/ATP');
    assert.equal(resolveWikilink('ＡＴＰ'), '/wiki/entities/ATP');
    assert.equal(resolveWikilink('Maße'), '/wiki/entities/ATP');
    assert.equal(resolveWikilink('οσ'), '/wiki/entities/ATP');
    assert.equal(resolveWikilink('ος'), '/wiki/entities/ATP');
    assert.equal(resolveWikilink('ATP#Energy charge'), '/wiki/entities/ATP#Energy%20charge');
    assert.deepEqual([...vault.forward.get('/wiki/entities/ATP')], ['/wiki/topics/Energy metabolism']);
  });

  it('loads fresh structured reading output without vocab pages', () => {
    const langs = loadLang();
    const entry = langs.find((lang) => lang.id === 'S1FRESHLANG00000000000000');

    assert.equal(entry?.slug, 'fresh-lang');
    assert.equal(entry?.title, 'Fresh Pipeline Reading');
    assert.equal(entry?.reading?.title, 'Fresh Pipeline Reading');
    assert.equal(entry?.chapters[0].heading, 'Fresh Chapter');
    assert.equal(entry?.word_count, 2);
    assert.equal(entry?.grammar_count, 1);
  });

  it('cleans ebook source titles for display', () => {
    assert.equal(cleanTitle('2026-07-08-my-book.mobi'), 'my book');
    assert.equal(cleanTitle('reference-volume.azw3'), 'reference volume');
  });

  it('loads source reader block artifacts and repairs neighbor links', () => {
    const doc = blocksForSource(SOURCE_ID);

    assert.equal(sourceHasBlocks(SOURCE_ID), true);
    assert.equal(doc.source_id, SOURCE_ID);
    assert.equal(doc.blocks.length, 4);
    assert.equal(doc.blocks[0].prev, '');
    assert.equal(doc.blocks[0].next, 'p-mid');
    assert.equal(doc.blocks[1].prev, 'p-dup');
    assert.equal(doc.blocks[2].next, 'p-tail');
  });

  it('extracts markdown source assets into reader blocks', () => {
    const doc = blocksForSource(MD_SOURCE_ID);

    assert.equal(sourceHasBlocks(MD_SOURCE_ID), true);
    assert.equal(doc.source_id, MD_SOURCE_ID);
    assert.equal(doc.title, 'Markdown Fixture Source');
    assert.equal(doc.lang, 'en');
    assert.equal(doc.blocks.length, 5);
    assert.deepEqual(doc.blocks.map((block) => block.type), ['heading', 'paragraph', 'paragraph', 'heading', 'paragraph']);
    assert.equal(doc.blocks[0].section, '\u7b2c\u4e00\u7ae0');
    assert.match(doc.blocks[1].text, /ATP stores energy/);
    assert.match(doc.blocks[2].text, /links catabolism to work/);
    assert.equal(doc.blocks[4].section, '\u7b2c\u4e8c\u7ae0');
    assert.match(doc.blocks[4].text, /Adenosine triphosphate turns over/);
    const href = sourceReaderHref(MD_SOURCE_ID, '\u7b2c\u4e8c\u7ae0');
    assert.equal(href.startsWith(`/sources/${MD_SOURCE_ID}/read/s-`), true);
    assert.equal(href.includes('#sec='), true);
  });

  it('indexes wiki citations by source id with anchors and clean excerpts', () => {
    const citations = citationsForSource(SOURCE_ID);

    assert.equal(citations.length, 1);
    assert.equal(citations[0].wiki_href, '/wiki/entities/ATP');
    assert.equal(citations[0].anchor, '\u7b2c\u4e00\u7ae0');
    assert.match(citations[0].excerpt, /claim above is drawn/);
  });

  it('indexes every source in multi-source citations with repeated prefixes', () => {
    const citations = citationsForSource(MD_SOURCE_ID);

    assert.equal(citations.length, 1);
    assert.equal(citations[0].wiki_href, '/wiki/entities/ATP');
    assert.equal(citations[0].anchor, '\u7b2c\u4e8c\u7ae0');
    assert.match(citations[0].excerpt, /claim above is drawn/);
  });
});
