const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../templates/progress.html'), 'utf8');
const source = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1].replace('    tick();', '');
const key = 'zukai.selection.v1.{{ job_id }}';
const items = [
  {index: 1, group: 1, variant: 'a', filename: 'diagram_001_a.png', section: '第1章'},
  {index: 2, group: 1, variant: 'b', filename: 'diagram_001_b.png', section: '第1章'},
  {index: 3, group: 2, variant: 'a', filename: 'diagram_002_a.png', section: '第2章'},
  {index: 4, group: 2, variant: 'b', filename: 'diagram_002_b.png', section: '第2章'},
  {index: 5, group: 3, filename: 'diagram_003.png', section: ''},
].map(item => ({...item, status: 'ok', excerpt: `原稿${item.index}`}));

function page(saved = null) {
  const elements = new Map(), storage = new Map(saved ? [[key, JSON.stringify(saved)]] : []);
  let payload = {items, available_images: 5, adopted: ['diagram_001_a__e1.png'], needs_revision: ['diagram_002_a.png']};
  let failPost = false, scroll = 0, requests = [], pendingItems = null;
  const element = id => {
    if (!elements.has(id)) {
      const classes = new Set();
      const node = {innerHTML: '', textContent: '', className: '', value: '', href: '', src: '', style: {},
        offsetHeight: 120, setAttribute(name, value) { this[name] = value; },
        getAttribute(name) { return this[name]; },
        classList: {add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c),
          toggle: (c, on) => on ? classes.add(c) : classes.delete(c)},
        querySelectorAll(selector) {
          if (selector !== '[data-selection-anchor]') return [];
          return [...this.innerHTML.matchAll(/data-selection-anchor="(\d+)"/g)].map((m, n) => ({
            getAttribute: () => m[1], classList: {add() {}, remove() {}},
            getBoundingClientRect: () => ({top: 192 + n * 300 - scroll, bottom: 460 + n * 300 - scroll}),
          }));
        },
      };
      elements.set(id, node);
    }
    return elements.get(id);
  };
  const context = vm.createContext({console, setTimeout: () => 0, clearTimeout() {},
    window: {innerHeight: 800, scrollBy: ({top}) => { scroll += top; }, addEventListener() {}},
    document: {getElementById: element, addEventListener() {}},
    localStorage: {getItem: k => storage.get(k), setItem: (k, value) => storage.set(k, value)},
    fetch: async (url, options) => {
      if (url.includes('/api/selection/')) {
        requests.push(options);
        if (failPost) return {ok: false, json: async () => ({error: '通信に失敗しました'})};
        const body = JSON.parse(options.body), marks = new Set(payload.needs_revision);
        body.needed ? marks.add(body.filename) : marks.delete(body.filename);
        payload = {...payload, needs_revision: [...marks]};
        return {ok: true, json: async () => ({needs_revision: [...marks]})};
      }
      if (pendingItems) { const wait = pendingItems; pendingItems = null; return wait; }
      return {ok: true, json: async () => payload};
    },
  });
  vm.runInContext(source, context);
  return {element, storage, requests, run: s => vm.runInContext(s, context), scroll: () => scroll,
    payload: p => {payload = p;}, fail: () => {failPost = true;}, delay: p => {pendingItems = p;}};
}

test('章で絞っても比較候補は揃い、全体の採用数・ZIPは変えない', async () => {
  const p = page(); await p.run('pollItems()');
  p.element('chapterFilter').value = '第2章'; p.element('selectionFilter').value = 'all';
  p.run('changeSelectionFilter()');
  assert.match(p.element('imgGrid').innerHTML, /002a/);
  assert.match(p.element('imgGrid').innerHTML, /002b/);
  assert.doesNotMatch(p.element('imgGrid').innerHTML, /001a|diagram_003/);
  assert.match(p.element('selectionCount').textContent, /表示 1 \/ 3箇所（2枚）/);
  assert.match(p.element('adoptSummary').textContent, /採用 1枚/);
  assert.match(p.element('downloadLabel').textContent, /5枚/);
});

test('未選定は箇所単位で判定し、手直し版の採用も含む', async () => {
  const p = page({status: 'unselected'}); await p.run('pollItems()');
  assert.doesNotMatch(p.element('imgGrid').innerHTML, /diagram_001/);
  assert.match(p.element('imgGrid').innerHTML, /diagram_002_a/);
  assert.match(p.element('imgGrid').innerHTML, /diagram_002_b/);
  assert.match(p.element('imgGrid').innerHTML, /diagram_003/);
});

test('要修正は印のある候補を含む箇所を表示し、採用では消えない', async () => {
  const p = page({status: 'revision'}); await p.run('pollItems()');
  assert.match(p.element('imgGrid').innerHTML, /diagram_002_a/);
  assert.match(p.element('imgGrid').innerHTML, /diagram_002_b/);
  p.payload({items, adopted: ['diagram_002_b.png'], needs_revision: ['diagram_002_a.png']});
  await p.run('pollItems()');
  assert.match(p.element('imgGrid').innerHTML, /要修正 ✓/);
  await p.run('toggleRevision(3)');
  assert.match(p.element('imgGrid').innerHTML, /条件に合う箇所はありません/);
  assert.ok(p.requests[0].headers['X-CSRF-Token']);
  assert.equal(JSON.parse(p.requests[0].body).needed, false);
});

test('章と選定状況を組み合わせ、該当ゼロから解除できる', async () => {
  const p = page({chapter: '第1章', status: 'unselected'}); await p.run('pollItems()');
  assert.match(p.element('imgGrid').innerHTML, /条件に合う箇所はありません/);
  p.run('resetSelectionFilters()');
  assert.match(p.element('selectionCount').textContent, /表示 3 \/ 3箇所（5枚）/);
  assert.equal(JSON.parse(p.storage.get(key)).status, 'all');
});

test('章情報のない旧ジョブも選定できる', async () => {
  const p = page({chapter: '__none__'}); await p.run('pollItems()');
  assert.match(p.element('chapterFilter').innerHTML, /章の情報なし/);
  assert.match(p.element('imgGrid').innerHTML, /diagram_003/);
  assert.doesNotMatch(p.element('imgGrid').innerHTML, /diagram_002/);
});

test('通信失敗時は印を付けたことにせず、再操作できる', async () => {
  const p = page(); await p.run('pollItems()'); p.fail();
  await p.run('toggleRevision(1)');
  assert.equal(p.run("revisionSet.has('diagram_001_a.png')"), false);
  assert.equal(p.run('revisionBusy'), false);
  assert.match(p.element('selectionError').textContent, /通信に失敗/);
  assert.match(p.element('modalRevisionError').textContent, /通信に失敗/);
});

test('章を変えたら絞り込んだ一覧の先頭へ移動する', async () => {
  const p = page({anchor: 5}); await p.run('pollItems()');
  assert.equal(p.scroll(), 600);
  p.element('chapterFilter').value = '第2章'; p.element('selectionFilter').value = 'all';
  p.run('changeSelectionFilter()');
  assert.equal(p.scroll(), 0);
  assert.match(p.element('selectionCount').textContent, /表示 1 \/ 3箇所/);
});

test('古いポーリング結果で保存した印を巻き戻さない', async () => {
  const p = page(); await p.run('pollItems()');
  let release; p.delay(new Promise(resolve => {release = resolve;}));
  const stale = p.run('pollItems()');
  await p.run('toggleRevision(1)');
  release({ok: true, json: async () => ({items, needs_revision: []})});
  await stale;
  assert.equal(p.run("revisionSet.has('diagram_001_a.png')"), true);
});

test('初回の描画後に位置を復元し、ポーリングではジャンプしない', async () => {
  const p = page({chapter: '', status: 'all', anchor: 3, offset: 0});
  await p.run('pollItems()'); assert.equal(p.scroll(), 300);
  await p.run('pollItems()'); assert.equal(p.scroll(), 300);
  p.run('rememberSelectionPosition()');
  assert.equal(JSON.parse(p.storage.get(key)).anchor, 3);
  assert.match(p.element('selectionResumeNote').textContent, /前回の位置から再開/);
});

test('前回の箇所が採用済みなら次の未選定の箇所から再開', async () => {
  const p = page({status: 'unselected', anchor: 1}); await p.run('pollItems()');
  assert.match(p.element('selectionResumeNote').textContent, /近くの箇所/);
  assert.doesNotMatch(p.element('imgGrid').innerHTML, /diagram_001/);
});

test('保存済み設定の不正な値を無視し、章名はエスケープする', async () => {
  const p = page({status: '<bad>', anchor: 'oops'});
  p.payload({items: [{...items[0], section: '<script>"test"</script>'}], adopted: []});
  await p.run('pollItems()');
  assert.equal(p.run('selectionView.status'), 'all');
  assert.equal(p.run('selectionView.anchor'), null);
  assert.doesNotMatch(p.element('chapterFilter').innerHTML, /<script>/);
  assert.match(p.element('chapterFilter').innerHTML, /&lt;script&gt;/);
});
