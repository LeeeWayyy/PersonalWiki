function normalizeLoose(value) {
  return String(value || '').replace(/\s+/g, '');
}

function normalizedTextMap(text) {
  const map = [];
  let norm = '';
  const src = String(text || '');
  for (let k = 0; k < src.length; k += 1) {
    if (!/\s/.test(src[k])) {
      map.push(k);
      norm += src[k];
    }
  }
  return { map, norm };
}

export function resolveDuplicateBlock(blocks, target) {
  const id = target?.block_id || '';
  if (!id) return null;
  const candidates = blocks.filter((block) => block.id === id);
  if (candidates.length <= 1) return candidates[0] || null;

  const ctx = target.context || {};
  const prev = ctx.prev_block_id || '';
  const next = ctx.next_block_id || '';
  const section = target.section_id || '';

  return candidates.find((block) => (
    (!section || block.section_id === section)
    && (block.prev || '') === prev
    && (block.next || '') === next
  )) || candidates.find((block) => (
    (block.prev || '') === prev
    && (block.next || '') === next
  )) || candidates[0] || null;
}

export function locateQuote(text, selector) {
  const source = String(text || '');
  const quote = selector?.quote || '';
  if (!quote) return null;

  const start = selector?.start;
  if (
    typeof start === 'number'
    && start >= 0
    && source.substring(start, start + quote.length) === quote
  ) {
    return { start, end: start + quote.length, exact: true };
  }

  const hits = [];
  for (let idx = source.indexOf(quote); idx >= 0; idx = source.indexOf(quote, idx + 1)) {
    hits.push(idx);
  }
  if (!hits.length) return null;
  const prefix = selector?.prefix || '';
  const suffix = selector?.suffix || '';
  const preferred = typeof start === 'number' && start >= 0 ? start : null;
  let best = hits[0];
  let bestScore = -Infinity;
  for (const idx of hits) {
    const before = source.slice(Math.max(0, idx - prefix.length), idx);
    const after = source.slice(idx + quote.length, idx + quote.length + suffix.length);
    let score = 0;
    if (prefix && before === prefix) score += 1000 + prefix.length;
    if (suffix && after === suffix) score += 1000 + suffix.length;
    if (preferred != null) score -= Math.abs(idx - preferred) / 1000;
    if (score > bestScore) {
      best = idx;
      bestScore = score;
    }
  }
  return { start: best, end: best + quote.length, exact: true };
}

function fuzzyLocateQuote(text, selector) {
  const quote = selector?.quote || '';
  const needle = normalizeLoose(quote);
  if (!needle) return null;

  const { map, norm } = normalizedTextMap(text);
  const exact = norm.indexOf(needle);
  if (exact >= 0) {
    return { start: map[exact], end: map[exact + needle.length - 1] + 1, fuzzy: true };
  }

  if (needle.length < 10) return null;
  const headLength = Math.max(6, Math.floor(needle.length * 0.6));
  const head = needle.slice(0, headLength);
  const partial = norm.indexOf(head);
  if (partial >= 0) {
    return { start: map[partial], end: map[partial + head.length - 1] + 1, fuzzy: true };
  }

  return null;
}

export function fuzzyLocateAnnotation(blocks, annotation, preferredBlock = null) {
  const target = annotation?.target || {};
  const selector = target.selector || {};
  const seen = new Set();
  const candidates = [];
  const byId = new Map(blocks.map((block) => [block.id, block]));

  const push = (block) => {
    if (block && !seen.has(block)) {
      seen.add(block);
      candidates.push(block);
    }
  };

  push(preferredBlock);
  push(resolveDuplicateBlock(blocks, target));
  push(byId.get(target.context?.prev_block_id || ''));
  push(byId.get(target.context?.next_block_id || ''));
  const section = target.section_id || preferredBlock?.section_id || '';
  blocks.filter((block) => !section || block.section_id === section).forEach(push);
  for (const block of blocks) push(block);

  for (const block of candidates) {
    const hit = fuzzyLocateQuote(block.text, selector);
    if (hit) return { block, ...hit };
  }
  return null;
}
