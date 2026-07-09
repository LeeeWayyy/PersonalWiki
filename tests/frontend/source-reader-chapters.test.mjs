import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
  chapterForSourceAnchor,
  sourceReaderHrefForAnchor,
  sourceReaderSections,
} from '../../src/lib/source-reader-chapters.mjs';

describe('source reader chapter helpers', () => {
  const blocks = [
    { id: 'p-a', section: 'Intro', section_id: 's-intro', text: 'A' },
    { id: 'p-b', section: 'Intro', section_id: 's-intro', text: 'B' },
    { id: 'p-c', section: '第一章', section_id: 's-one', text: 'C' },
    { id: 'p-d', section: 'Intro', section_id: 's-intro', text: 'D' },
  ];

  it('groups consecutive blocks and disambiguates duplicate section routes', () => {
    const chapters = sourceReaderSections(blocks);

    assert.deepEqual(chapters.map((chapter) => chapter.title), ['Intro', '第一章', 'Intro']);
    assert.deepEqual(chapters.map((chapter) => chapter.id), ['s-intro-1', 's-one', 's-intro-3']);
    assert.equal(chapters[0].block_count, 2);
  });

  it('routes anchors to the matching chapter and preserves fragments', () => {
    const chapters = sourceReaderSections(blocks);

    assert.equal(chapterForSourceAnchor(chapters, '第一章').id, 's-one');
    assert.equal(chapterForSourceAnchor(chapters, 'p-d').id, 's-intro-3');
    assert.equal(chapterForSourceAnchor(chapters, '#b=p-d').id, 's-intro-3');
    assert.equal(
      sourceReaderHrefForAnchor('S1', chapters, '第一章'),
      '/sources/S1/read/s-one#sec=%E7%AC%AC%E4%B8%80%E7%AB%A0',
    );
  });
});
