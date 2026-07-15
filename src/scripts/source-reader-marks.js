export function charRange(root, start, end) {
  const range = document.createRange();
  let position = 0;
  let found = 0;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const length = node.nodeValue.length;
    if (!(found & 1) && start <= position + length) {
      range.setStart(node, start - position);
      found |= 1;
    }
    if (!(found & 2) && end <= position + length) {
      range.setEnd(node, end - position);
      found |= 2;
      break;
    }
    position += length;
  }
  return found === 3 ? range : null;
}

export function wrapTextSegments(range, className, annotationId, title = '') {
  const root = range.commonAncestorContainer.nodeType === Node.TEXT_NODE
    ? range.commonAncestorContainer.parentNode
    : range.commonAncestorContainer;
  const nodes = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue || !range.intersectsNode(node)) return NodeFilter.FILTER_REJECT;
      const start = node === range.startContainer ? range.startOffset : 0;
      const end = node === range.endContainer ? range.endOffset : node.nodeValue.length;
      return end > start ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  let node;
  while ((node = walker.nextNode())) {
    nodes.push({
      node,
      start: node === range.startContainer ? range.startOffset : 0,
      end: node === range.endContainer ? range.endOffset : node.nodeValue.length,
    });
  }
  let wrapped = 0;
  for (const part of nodes) {
    try {
      const segment = document.createRange();
      segment.setStart(part.node, part.start);
      segment.setEnd(part.node, part.end);
      const mark = document.createElement('mark');
      mark.className = className;
      mark.dataset.aid = annotationId;
      if (title) mark.title = title;
      segment.surroundContents(mark);
      wrapped++;
    } catch {}
  }
  return wrapped;
}
