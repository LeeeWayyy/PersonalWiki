import { defineConfig } from 'astro/config';
import remarkZones from './src/plugins/remark-zones.mjs';
import remarkMermaid from './src/plugins/remark-mermaid.mjs';
import remarkInline from './src/plugins/remark-inline.mjs';
import remarkStripH1 from './src/plugins/remark-strip-h1.mjs';
import remarkRawHtmlGuard from './src/plugins/remark-raw-html-guard.mjs';

const remarkPlugins = [remarkStripH1, remarkRawHtmlGuard, remarkMermaid, remarkZones, remarkInline];
const backend = `http://${process.env.PW_HOST || '127.0.0.1'}:${process.env.PW_PORT || '8787'}`;

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

// Production is served by FastAPI. These proxies keep Astro dev same-origin too.
export default defineConfig({
  site: 'http://localhost:4321',
  markdown: {
    processor: markdownProcessor,
    shikiConfig: { theme: 'github-dark', wrap: true },
  },
  devToolbar: { enabled: false },
  vite: {
    server: {
      proxy: Object.fromEntries([
        '/health', '/ingest', '/jobs', '/preflight', '/vocab', '/review', '/export',
        '/translate', '/assist', '/annotations', '/media', '/lang',
        '/wiki/human-zone', '/wiki/page/remove',
      ].map((path) => [path, backend])),
    },
  },
});
