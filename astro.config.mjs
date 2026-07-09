import { defineConfig } from 'astro/config';
import remarkZones from './src/plugins/remark-zones.mjs';
import remarkMermaid from './src/plugins/remark-mermaid.mjs';
import remarkInline from './src/plugins/remark-inline.mjs';
import remarkStripH1 from './src/plugins/remark-strip-h1.mjs';
import remarkRawHtmlGuard from './src/plugins/remark-raw-html-guard.mjs';

const remarkPlugins = [remarkStripH1, remarkRawHtmlGuard, remarkMermaid, remarkZones, remarkInline];

const markdownProcessor = {
  name: 'personal-wiki-markdown',
  options: {},
  async createRenderer(shared) {
    const { createMarkdownProcessor } = await import('@astrojs/markdown-remark');
    return createMarkdownProcessor({
      ...shared,
      gfm: true,
      remarkPlugins,
    });
  },
};

// Private personal site — served locally, reached over Tailscale. No SSR needed
// for the reading layer (the ingest/study backend is a separate FastAPI service).
export default defineConfig({
  site: 'http://localhost:4321',
  markdown: {
    processor: markdownProcessor,
    shikiConfig: { theme: 'github-dark', wrap: true },
  },
  devToolbar: { enabled: false },
});
