// Shared client helpers: HTML escape + query highlight, and the filter/view
// logic behind the .et-* list pages (Entities & Topics, Sources).

export const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

export function hl(text, q) {
  if (!q) return esc(text);
  const i = text.toLowerCase().indexOf(q);
  if (i < 0) return esc(text);
  return esc(text.slice(0, i)) + '<mark>' + esc(text.slice(i, i + q.length)) + '</mark>' + esc(text.slice(i + q.length));
}

// Wires search box, clear button, group rail(s), section counts, empty state,
// and the card/list view toggle for a page of .et-card items.
// cfg: { root, search, clearBtn, countEl?, emptyEl, view, viewCards, viewList,
//        tab?, emptyText?(query, rawValue), onApplied?(state), selector overrides }
// Returns { state, apply, syncClear, syncGroup } so tabbed pages (Entities &
// Topics) can drive tab switches themselves.
export function mountListFilter(cfg) {
  const { root, search, clearBtn, countEl, emptyEl, view, viewCards, viewList, emptyText, onApplied } = cfg;
  const cardSelector = cfg.cardSelector || '.et-card';
  const sectionSelector = cfg.sectionSelector || '.et-section';
  const railSelector = cfg.railSelector || '.et-rail';
  const groupButtonSelector = cfg.groupButtonSelector || '.et-gbtn';
  const titleSelector = cfg.titleSelector || '.t';
  const groupActiveClass = cfg.groupActiveClass || 'on';
  const resultLabel = cfg.resultLabel || '结果';
  const cards = [...root.querySelectorAll(cardSelector)];
  const sections = [...root.querySelectorAll(sectionSelector)];
  const rails = [...root.querySelectorAll(railSelector)];
  const state = { tab: cfg.tab || '', group: 'all', query: '' };
  const groupsForCard = cfg.groupsForCard || ((card) => (card.dataset.group ? [card.dataset.group] : []));
  const groupMatches = cfg.groupMatches || ((card, group) => groupsForCard(card).includes(group));

  const inTab = (el) => !state.tab || el.dataset.tab === state.tab;
  const curRail = () => rails.find(inTab);

  function apply() {
    const gcount = {};
    let queryMatched = 0;
    let shown = 0;
    for (const c of cards) {
      const tok = inTab(c);
      const qok = !state.query || (c.dataset.search || '').includes(state.query);
      const gok = state.group === 'all' || groupMatches(c, state.group);
      const vis = tok && qok && gok;
      c.style.display = vis ? '' : 'none';
      if (tok && qok) {
        queryMatched++;
        for (const group of groupsForCard(c)) gcount[group] = (gcount[group] || 0) + 1;
      }
      if (vis) { shown++; const t = c.querySelector(titleSelector); if (t) t.innerHTML = hl(c.dataset.t || '', state.query); }
    }
    for (const s of sections) {
      if (!inTab(s)) { s.style.display = 'none'; continue; }
      const vs = [...s.querySelectorAll(cardSelector)].filter((c) => c.style.display !== 'none');
      s.style.display = vs.length ? '' : 'none';
      const c = s.querySelector('.et-shead .c'); if (c) c.textContent = String(vs.length);
    }
    curRail()?.querySelectorAll(groupButtonSelector).forEach((b) => {
      const g = b.dataset.group, c = b.querySelector('.c, .n');
      if (c) c.textContent = String(g === 'all' ? queryMatched : (gcount[g] || 0));
    });
    if (countEl) countEl.textContent = shown + ' ' + resultLabel;
    emptyEl.style.display = shown ? 'none' : '';
    if (emptyText) emptyEl.textContent = emptyText(state.query, search.value.trim());
    if (onApplied) onApplied(state);
  }

  function syncClear() { clearBtn.style.display = state.query ? '' : 'none'; }
  function syncGroup() {
    curRail()?.querySelectorAll(groupButtonSelector).forEach((b) => b.classList.toggle(groupActiveClass, b.dataset.group === state.group));
  }

  search.addEventListener('input', () => { state.query = search.value.trim().toLowerCase(); syncClear(); apply(); });
  clearBtn.addEventListener('click', () => { search.value = ''; state.query = ''; syncClear(); apply(); search.focus(); });

  rails.forEach((rail) => rail.addEventListener('click', (e) => {
    const b = e.target instanceof Element ? e.target.closest('.et-gbtn') : null;
    if (!b) return;
    state.group = b.dataset.group; syncGroup(); apply();
  }));

  function setView(list) {
    view.classList.toggle('list', list);
    viewCards.classList.toggle('on', !list); viewList.classList.toggle('on', list);
  }
  viewCards.addEventListener('click', () => setView(false));
  viewList.addEventListener('click', () => setView(true));

  return { state, apply, syncClear, syncGroup };
}
