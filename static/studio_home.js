'use strict';
(() => {
  const search = document.getElementById('history-search');
  if (!search) return;
  const rows = [...document.querySelectorAll('[data-history-job]')];
  const buttons = [...document.querySelectorAll('[data-history-filter]')];
  const clear = document.getElementById('history-clear');
  const storageKey = 'zukai.home.history.v1';
  let filter = 'all';
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) || '{}');
    if (typeof saved.query === 'string') search.value = saved.query.slice(0, 300);
    if (['all','working','completed','attention'].includes(saved.filter)) filter = saved.filter;
  } catch (_) { /* Optional view state; the production records are unchanged. */ }
  function render() {
    const terms = search.value.normalize('NFKC').toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
    let count = 0;
    rows.forEach(row => {
      const haystack = row.dataset.search.normalize('NFKC').toLocaleLowerCase();
      row.hidden = (filter !== 'all' && row.dataset.bucket !== filter) || !terms.every(term => haystack.includes(term));
      if (!row.hidden) count++;
    });
    buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.historyFilter === filter)));
    document.getElementById('history-count').textContent = count + ' / ' + rows.length + ' 件を表示';
    document.getElementById('history-empty').hidden = count > 0;
    clear.hidden = !terms.length && filter === 'all';
    try { sessionStorage.setItem(storageKey, JSON.stringify({query:search.value, filter})); } catch (_) {}
  }
  function reset() { search.value = ''; filter = 'all'; render(); search.focus(); }
  search.addEventListener('input', render);
  search.addEventListener('keydown', event => { if (event.key === 'Escape' && search.value) { event.preventDefault(); reset(); } });
  clear.addEventListener('click', reset);
  document.getElementById('history-reset').addEventListener('click', reset);
  buttons.forEach(button => button.addEventListener('click', () => { filter = button.dataset.historyFilter; render(); }));
  render();
})();
