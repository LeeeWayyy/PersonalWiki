#!/usr/bin/env node
// serve.mjs — static server for the built `dist/` that sets the security response
// headers `astro preview` cannot. The app's <meta> CSP already blocks third-party
// loads, but `frame-ancestors` is ignored in <meta>; `X-Frame-Options: DENY` here
// provides the equivalent clickjacking protection as a real header. Dependency-free.
//
//   node scripts/serve.mjs --host 127.0.0.1 --port 4321   (root = ./dist)
import { createServer } from 'node:http';
import { stat, readFile } from 'node:fs/promises';
import { join, normalize, extname, resolve } from 'node:path';

const args = process.argv.slice(2);
const opt = (name, def) => { const i = args.indexOf(name); return i >= 0 && args[i + 1] ? args[i + 1] : def; };
const HOST = opt('--host', process.env.SITE_HOST || '127.0.0.1');
const PORT = parseInt(opt('--port', process.env.SITE_PORT || '4321'), 10);
const ROOT = resolve(process.cwd(), opt('--root', 'dist'));

const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
  '.webp': 'image/webp', '.avif': 'image/avif', '.ico': 'image/x-icon',
  '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf', '.otf': 'font/otf',
  '.wasm': 'application/wasm', '.txt': 'text/plain; charset=utf-8',
  '.map': 'application/json; charset=utf-8', '.xml': 'application/xml',
};

// Security headers on every response. These complement the app's <meta> CSP.
const SECURITY_HEADERS = {
  'X-Frame-Options': 'DENY',                 // <meta> can't do CSP frame-ancestors
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'Cross-Origin-Opener-Policy': 'same-origin',
};

function setHeaders(res, extra = {}) {
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) res.setHeader(k, v);
  for (const [k, v] of Object.entries(extra)) res.setHeader(k, v);
}

async function resolveFile(urlPath) {
  // Decode, strip query, block traversal, map directory/extensionless → index.html.
  let p = decodeURIComponent(urlPath.split('?')[0]);
  const full = normalize(join(ROOT, p));
  if (full !== ROOT && !full.startsWith(ROOT + '/')) return null; // traversal guard
  const candidates = [];
  if (p.endsWith('/')) candidates.push(join(full, 'index.html'));
  else if (!extname(p)) candidates.push(join(full, 'index.html'), full);
  else candidates.push(full);
  for (const c of candidates) {
    try { const s = await stat(c); if (s.isFile()) return c; } catch { /* next */ }
  }
  return null;
}

const server = createServer(async (req, res) => {
  try {
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      setHeaders(res); res.writeHead(405); return res.end('Method Not Allowed');
    }
    const file = await resolveFile(req.url || '/');
    if (!file) {
      let body = 'Not Found';
      try { body = await readFile(join(ROOT, '404.html')); } catch { /* plain */ }
      setHeaders(res, { 'Content-Type': 'text/html; charset=utf-8' });
      res.writeHead(404); return res.end(req.method === 'HEAD' ? undefined : body);
    }
    const data = await readFile(file);
    setHeaders(res, { 'Content-Type': MIME[extname(file).toLowerCase()] || 'application/octet-stream' });
    res.writeHead(200);
    res.end(req.method === 'HEAD' ? undefined : data);
  } catch (e) {
    setHeaders(res); res.writeHead(500); res.end('Internal Server Error');
  }
});

server.listen(PORT, HOST, () => {
  console.log(`serving ${ROOT} → http://${HOST}:${PORT}  (security headers on)`);
});
