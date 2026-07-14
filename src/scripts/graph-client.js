// Interactive link-graph rendered into a panel: filter by direction, zoom,
// and click a node to inspect it. Fed the same pageNeighbors data as the rail
// thumbnail (full titles + en alias + direction), no external deps.
import { t } from '../lib/i18n.mjs';

const CX = 310, CY = 220, R = 155;
const colorOf = (d) => (d === 'out' ? '#d3835a' : d === 'in' ? '#8faa7e' : '#e0ab5f');
const esc = (s) => (s + '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const att = (s) => esc(s).replace(/"/g, '&quot;');

export function mountGraph(container, data) {
  const list = data.nodes || [];
  const nodes = list.map((n, i) => {
    const ang = (-90 + i * (360 / Math.max(list.length, 1))) * Math.PI / 180;
    return { ...n, x: CX + R * Math.cos(ang), y: CY + R * Math.sin(ang) };
  });
  const counts = { all: nodes.length, out: 0, in: 0, both: 0 };
  for (const n of nodes) counts[n.dir]++;
  let filter = 'all', zoom = 1, picked = null;

  container.innerHTML = `
    <div class="gph-head">
      <span class="gph-title">${esc(t('graph.title'))}</span>
      <div class="gph-filter">
        <button data-f="all">${esc(t('word.all'))} <span class="c">${counts.all}</span></button>
        <button data-f="out">${esc(t('graph.fOut'))} <span class="c">${counts.out}</span></button>
        <button data-f="in">${esc(t('graph.fIn'))} <span class="c">${counts.in}</span></button>
        <button data-f="both">${esc(t('graph.fBoth'))} <span class="c">${counts.both}</span></button>
      </div>
      <div class="gph-zoom">
        <button data-z="out" aria-label="Zoom out">−</button>
        <span class="gph-pct">100%</span>
        <button data-z="in" aria-label="Zoom in">+</button>
        <button data-z="reset">reset</button>
      </div>
      <button class="gph-close" data-gph-close aria-label="Close">✕</button>
    </div>
    <div class="gph-canvas">
      <svg viewBox="0 0 620 440" role="img" aria-label="${att(t('graph.title'))}"><g class="gph-g"></g></svg>
      <div class="gph-inspect" hidden></div>
    </div>`;

  const g = container.querySelector('.gph-g');
  const pct = container.querySelector('.gph-pct');
  const inspect = container.querySelector('.gph-inspect');

  function draw() {
    const parts = [];
    nodes.forEach((n) => {
      const on = filter === 'all' || n.dir === filter;
      parts.push(`<line x1="${CX}" y1="${CY}" x2="${n.x.toFixed(1)}" y2="${n.y.toFixed(1)}" stroke="${colorOf(n.dir)}" stroke-width="1.4" opacity="${on ? 0.5 : 0.08}"/>`);
    });
    nodes.forEach((n, i) => {
      const on = filter === 'all' || n.dir === filter;
      const isPick = picked === i;
      const lower = n.y > CY;
      const ty = n.y + (lower ? 23 : -13);
      const lw = Math.max(24, [...(n.title || '')].length * 13 + 10);
      parts.push(`<g class="gph-node" data-i="${i}" opacity="${on ? 1 : 0.16}">
        <circle cx="${n.x.toFixed(1)}" cy="${n.y.toFixed(1)}" r="${isPick ? 9 : 6.5}" fill="${colorOf(n.dir)}" stroke="${isPick ? '#efe7da' : '#15120d'}" stroke-width="2"/>
        <rect x="${(n.x - lw / 2).toFixed(1)}" y="${(ty - 12).toFixed(1)}" width="${lw}" height="17" rx="5" fill="#15120d" opacity=".78"/>
        <text x="${n.x.toFixed(1)}" y="${ty.toFixed(1)}" text-anchor="middle" font-size="12" fill="#d8ccb8" font-family="Songti SC, serif">${esc(n.title)}</text>
      </g>`);
    });
    parts.push(`<circle cx="${CX}" cy="${CY}" r="15" fill="#efe7da" stroke="#15120d" stroke-width="3"/>
      <text x="${CX}" y="${CY + 33}" text-anchor="middle" font-size="14" font-weight="600" fill="#efe7da" font-family="Songti SC, serif">${esc([...(data.title || '')].slice(0, 10).join(''))}</text>`);
    g.innerHTML = parts.join('');
    g.setAttribute('transform', `translate(${CX},${CY}) scale(${zoom}) translate(${-CX},${-CY})`);
    pct.textContent = Math.round(zoom * 100) + '%';
    container.querySelectorAll('.gph-filter button').forEach((b) => b.classList.toggle('on', b.dataset.f === filter));
  }

  function showInspect() {
    if (picked == null) { inspect.hidden = true; return; }
    const n = nodes[picked];
    const dirKey = n.dir === 'out' ? 'graph.dirOut' : n.dir === 'in' ? 'graph.dirIn' : 'graph.dirBoth';
    inspect.hidden = false;
    inspect.innerHTML = `<div class="gph-i-title">${esc(n.title)}</div>` +
      (n.en ? `<div class="gph-i-en">${esc(n.en)}</div>` : '') +
      `<div class="gph-i-dir"><span class="dot" style="background:${colorOf(n.dir)}"></span><span>${esc(t(dirKey))}</span><a href="${att(n.href)}">${esc(t('graph.open'))}</a></div>`;
  }

  container.querySelector('.gph-filter').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-f]'); if (!b) return;
    filter = b.dataset.f; draw();
  });
  container.querySelector('.gph-zoom').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-z]'); if (!b) return;
    if (b.dataset.z === 'in') zoom = Math.min(2.4, +(zoom + 0.2).toFixed(2));
    else if (b.dataset.z === 'out') zoom = Math.max(0.6, +(zoom - 0.2).toFixed(2));
    else zoom = 1;
    draw();
  });
  g.addEventListener('click', (e) => {
    const node = e.target.closest('.gph-node'); if (!node) return;
    picked = Number(node.dataset.i); draw(); showInspect();
  });

  draw();
}
