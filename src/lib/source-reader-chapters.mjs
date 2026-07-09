const FRONT_MATTER_LABEL = 'Front matter';

function cleanPart(value) {
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9\u4e00-\u9fff\u3040-\u30ff_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64);
}

function baseChapterId(section, index) {
  const sectionId = cleanPart(section.section_id);
  if (sectionId) return sectionId;
  const label = cleanPart(section.section || '');
  return label || `front-matter-${index + 1}`;
}

export function sourceReaderSections(blocks = []) {
  const sections = [];
  for (const block of blocks || []) {
    const last = sections[sections.length - 1];
    if (!last || last.section !== block.section || last.section_id !== block.section_id) {
      sections.push({
        section: block.section || '',
        section_id: block.section_id || '',
        blocks: [],
      });
    }
    sections[sections.length - 1].blocks.push(block);
  }

  const bases = sections.map(baseChapterId);
  const counts = new Map();
  for (const base of bases) counts.set(base, (counts.get(base) || 0) + 1);
  const seen = new Map();

  return sections.map((section, index) => {
    const base = bases[index];
    const duplicate = counts.get(base) > 1;
    const occurrence = seen.get(base) || 0;
    seen.set(base, occurrence + 1);
    const id = duplicate ? `${base}-${index + 1}` : base;
    return {
      ...section,
      id,
      index,
      title: section.section || FRONT_MATTER_LABEL,
      first_block_id: section.blocks[0]?.id || '',
      block_count: section.blocks.length,
    };
  });
}

export function sourceReaderChapterHref(sourceId, chapter, fragment = '') {
  const base = `/sources/${encodeURIComponent(sourceId)}/read/${encodeURIComponent(chapter.id)}`;
  return fragment ? `${base}#${fragment}` : base;
}

function decodeLoose(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function normalizeSourceAnchor(anchor = '') {
  let raw = String(anchor || '').trim();
  if (raw.startsWith('#')) raw = raw.slice(1);
  return decodeLoose(raw);
}

function fragmentForSourceAnchor(anchor = '') {
  const raw = normalizeSourceAnchor(anchor);
  if (!raw) return '';
  if (raw.includes('=')) return encodeURI(raw);
  if (raw.startsWith('s-')) return `s=${encodeURIComponent(raw)}`;
  if (raw.startsWith('p-') || raw.startsWith('h-')) return encodeURIComponent(raw);
  return `sec=${encodeURIComponent(raw)}`;
}

export function chapterForSourceAnchor(chapters = [], anchor = '') {
  if (!chapters.length) return null;
  const raw = normalizeSourceAnchor(anchor);
  if (!raw) return chapters[0];

  let sectionId = '';
  let label = '';
  let blockId = '';

  if (raw.includes('=')) {
    const params = new URLSearchParams(raw);
    sectionId = params.get('s') || '';
    label = params.get('sec') || '';
    blockId = params.get('b') || '';
  } else if (raw.startsWith('s-')) {
    sectionId = raw;
  } else if (raw.startsWith('p-') || raw.startsWith('h-') || raw.startsWith('i-')) {
    blockId = raw;
  } else {
    label = raw;
  }

  if (sectionId) {
    const hit = chapters.find((chapter) => chapter.section_id === sectionId);
    if (hit) return hit;
  }
  if (label) {
    const hit = chapters.find((chapter) => chapter.section === label || chapter.title === label);
    if (hit) return hit;
  }
  if (blockId) {
    const hit = chapters.find((chapter) => chapter.blocks.some((block) => block.id === blockId));
    if (hit) return hit;
  }
  return chapters[0];
}

export function sourceReaderHrefForAnchor(sourceId, chapters = [], anchor = '') {
  const fragment = fragmentForSourceAnchor(anchor);
  if (chapters.length <= 1) {
    const base = `/sources/${encodeURIComponent(sourceId)}/read`;
    return fragment ? `${base}#${fragment}` : base;
  }
  const chapter = chapterForSourceAnchor(chapters, anchor) || chapters[0];
  return sourceReaderChapterHref(sourceId, chapter, fragment);
}
