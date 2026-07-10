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
  if (raw.startsWith('sec=')) return `sec=${decodeLoose(raw.slice(4))}`;
  if (raw.startsWith('s=')) return `s=${decodeLoose(raw.slice(2))}`;
  if (raw.startsWith('b=') || raw.includes('&')) return raw;
  return decodeLoose(raw);
}

function sourceAnchorParts(anchor = '') {
  const original = String(anchor || '').trim().replace(/^#/, '');
  const raw = normalizeSourceAnchor(anchor);
  const out = { raw, sectionId: '', label: '', blockId: '', prev: '', next: '' };
  if (!raw) return out;
  if (raw.startsWith('sec=')) {
    out.label = raw.slice(4);
  } else if (raw.startsWith('s=')) {
    out.sectionId = raw.slice(2);
  } else if (original.startsWith('b=')) {
    const params = new URLSearchParams(raw);
    out.sectionId = params.get('s') || '';
    out.label = params.get('sec') || '';
    out.blockId = params.get('b') || '';
    out.prev = params.get('prev') || '';
    out.next = params.get('next') || '';
  } else if (raw.startsWith('s-')) {
    out.sectionId = raw;
  } else if (raw.startsWith('p-') || raw.startsWith('h-') || raw.startsWith('i-')) {
    out.blockId = raw;
  } else {
    out.label = raw;
  }
  return out;
}

function comparableLabel(value = '') {
  return String(value || '').normalize('NFKC').toLocaleLowerCase().replace(/\s+/g, ' ').trim();
}

function chapterLabels(chapter) {
  return [...new Set([chapter.section, chapter.title].map(comparableLabel).filter(Boolean))];
}

function findChapterByLooseLabel(chapters, label) {
  const needle = comparableLabel(label);
  if (!needle) return null;
  const startsWithHit = chapters.find((chapter) =>
    chapterLabels(chapter).some((candidate) => candidate.startsWith(needle) || needle.startsWith(candidate)));
  if (startsWithHit) return startsWithHit;
  return chapters.find((chapter) =>
    chapterLabels(chapter).some((candidate) => candidate.includes(needle) || needle.includes(candidate))) || null;
}

function fragmentForSourceAnchor(anchor = '') {
  const { raw } = sourceAnchorParts(anchor);
  if (!raw) return '';
  if (raw.startsWith('sec=')) return `sec=${encodeURIComponent(raw.slice(4))}`;
  if (raw.startsWith('s=')) return `s=${encodeURIComponent(raw.slice(2))}`;
  if (raw.startsWith('b=')) return raw;
  if (raw.startsWith('s-')) return `s=${encodeURIComponent(raw)}`;
  if (raw.startsWith('p-') || raw.startsWith('h-')) return encodeURIComponent(raw);
  return `sec=${encodeURIComponent(raw)}`;
}

export function chapterForSourceAnchor(chapters = [], anchor = '') {
  if (!chapters.length) return null;
  const { raw, sectionId, label, blockId } = sourceAnchorParts(anchor);
  if (!raw) return chapters[0];

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
  if (label) {
    const hit = findChapterByLooseLabel(chapters, label);
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
