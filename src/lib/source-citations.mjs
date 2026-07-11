// Source-citation contract shared by the remark renderer and vault index.
// Canonical section labels are encoded before citation-list delimiters are read.

function encodeRFC3986(value) {
  return encodeURIComponent(String(value)).replace(/[!'()*]/g, (char) =>
    `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
}

function decodeLoose(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

export function encodeSectionAnchor(label) {
  return `sec=${encodeRFC3986(label)}`;
}

export function decodeSourceAnchor(anchor = '') {
  const value = String(anchor || '').trim().replace(/^#/, '');
  return value.startsWith('sec=') ? decodeLoose(value.slice(4)) : value;
}

export function sourceCitationRef(sourceId, sectionLabel = '') {
  const ref = `src:${sourceId}`;
  return sectionLabel ? `${ref}#${encodeSectionAnchor(sectionLabel)}` : ref;
}

export function citationParts(value) {
  return String(value || '')
    .split(',')
    .map((part) => part.trim().replace(/^src:\s*/i, ''))
    .filter(Boolean)
    .map((part) => {
      const hash = part.indexOf('#');
      if (hash < 0) return { id: part.trim(), anchor: '', rawAnchor: '' };
      const rawAnchor = part.slice(hash + 1).trim();
      return {
        id: part.slice(0, hash).trim(),
        anchor: decodeSourceAnchor(rawAnchor),
        rawAnchor,
      };
    })
    .filter((part) => part.id);
}
