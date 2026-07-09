import {
  fuzzyLocateAnnotation,
  locateQuote,
  resolveDuplicateBlock,
} from '../lib/source-reader-anchors.mjs';

function blockRecord(el) {
  return {
    id: el.dataset.block || '',
    section_id: el.dataset.section || '',
    prev: el.dataset.prev || '',
    next: el.dataset.next || '',
    text: el.textContent || '',
    el,
  };
}

function allBlocks(doc) {
  return [...doc.querySelectorAll('[data-block]')].map(blockRecord);
}

function textBlocks(doc) {
  return [...doc.querySelectorAll('.blk')].map(blockRecord);
}

function resolveBlock(doc, annotation) {
  const hit = resolveDuplicateBlock(allBlocks(doc), annotation?.target || {});
  return hit?.el || null;
}

function fuzzyLocate(doc, annotation, preferredEl = null) {
  const blocks = textBlocks(doc);
  const preferredBlock = blocks.find((block) => block.el === preferredEl) || null;
  const hit = fuzzyLocateAnnotation(blocks, annotation, preferredBlock);
  return hit ? { el: hit.block.el, start: hit.start, end: hit.end } : null;
}

export function installSourceReaderAnchors(global = window) {
  global.SourceReaderAnchors = {
    fuzzyLocate,
    locateQuote,
    resolveBlock,
  };
}
