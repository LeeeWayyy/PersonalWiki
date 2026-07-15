// Fail the build if vault Markdown contains raw HTML tags. The pipeline uses
// HTML comments as zone markers; those are allowed and rewritten later by
// remark-zones. Actual HTML tags should live in Astro components, not content.
import { visit } from 'unist-util-visit';

const ZONE_COMMENT_RE = /^<!--\s*\/?[a-z][a-z-]*-zone\s*-->$/;

export default function remarkRawHtmlGuard() {
  return (tree, file) => {
    visit(tree, 'html', (node) => {
      const raw = String(node.value || '').trim();
      if (!raw || ZONE_COMMENT_RE.test(raw)) return;
      file.fail(`Raw HTML is not allowed in vault Markdown: ${raw.slice(0, 80)}`, node);
    });
  };
}
