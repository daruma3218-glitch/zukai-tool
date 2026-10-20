// 地図の箇所の「地図をやめて作り直す」と、拡大画面の「別の種類で作り直す」（2026-10-14）
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../templates/progress.html'), 'utf8');
const source = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1].replace('    tick();', '');

function page(itemsPayload, editResponse = null) {
  const elements = new Map();
  const posts = [];
  const element = id => {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {
        textContent: '', innerHTML: '', style: {}, href: '', className: '', value: '', src: '',
        classList: {
          toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
          contains: name => classes.has(name), add: name => classes.add(name), remove: name => classes.delete(name),
        },
        querySelectorAll: () => [],
        setAttribute() {}, focus() {},
      });
    }
    return elements.get(id);
  };
  const context = vm.createContext({
    window: {}, console, setTimeout: () => 0, clearTimeout() {},
    localStorage: { getItem: () => null, setItem() {} },
    document: { getElementById: element, addEventListener() {} },
    fetch: async (url, options = {}) => {
      if (options.method === 'POST') {
        posts.push({ url, body: JSON.parse(options.body || '{}') });
        const reply = editResponse || { status: 200, data: { edit: { output: 'diagram_001__e1.png' }, edits: [] } };
        return { ok: reply.status < 400, status: reply.status, json: async () => reply.data };
      }
      return { ok: true, json: async () => url.includes('/api/items/') ? itemsPayload
        : { status: 'completed', succeeded: 2, phase: 3, percent: 100 } };
    },
  });
  vm.runInContext(source, context);
  return { element, posts, run: command => vm.runInContext(command, context) };
}

const mapJob = {
  available_images: 2,
  items: [
    { index: 1, status: 'ok', filename: 'diagram_001.png', type: 'map', render: 'map', variant_label: '地図',
      map_alt: 'realphoto', excerpt: '首都にある像を郊外の墓地へ移すことに決めました。' },
    { index: 2, status: 'ok', filename: 'diagram_002.png', type: 'diagram', variant_label: '図解',
      excerpt: '制度の仕組みを説明します。' },
  ],
  adopted: [], edits: [], needs_revision: [],
};

function card(p, idx) {
  const grid = p.element('imgGrid').innerHTML;
  const start = grid.indexOf(`id="card-${idx}"`);
  const next = grid.indexOf('id="card-', start + 1);
  return grid.slice(start, next === -1 ? undefined : next);
}

test('地図の箇所だけに「地図をやめて作り直す」を出し、おすすめの種類を示す', async () => {
  const p = page(mapJob);
  await p.run('pollItems()');
  const mapCard = card(p, 1);
  assert.match(mapCard, /地図をやめて作り直す（おすすめ：実写風）/);
  assert.match(mapCard, /retypeImage\(1, 'diagram'\)/);
  assert.match(mapCard, /retypeImage\(1, 'illustration'\)/);
  assert.match(mapCard, /retypeImage\(1, 'realphoto'\)/);
  assert.match(mapCard, /bg-sky-700 text-white[^>]*>実写風</);  // おすすめは濃い色
  assert.doesNotMatch(card(p, 2), /地図をやめて作り直す/);
});

test('ボタン1つで作り直しを頼み、作成中はそのボタンを止める', async () => {
  const p = page(mapJob, { status: 200, data: { edit: { output: 'diagram_001__e1.png' }, edits: [
    { id: 'r1', source: 'diagram_001.png', output: 'diagram_001__e1.png', action: 'retype',
      target_type: 'diagram', label: '図解で作り直し', status: 'queued' }] } });
  await p.run('pollItems()');
  await p.run("retypeImage(1, 'diagram')");
  assert.deepEqual(p.posts[0], { url: '/api/edit/{{ job_id }}',
    body: { source: 'diagram_001.png', action: 'retype', target_type: 'diagram' } });
  const mapCard = card(p, 1);
  assert.match(mapCard, /disabled[^>]*>作成中…</);
  assert.match(mapCard, /作り直し中/);
});

test('作り直しが済んだら、カードはその版を出し、☆はその版を採用する', async () => {
  const done = { ...mapJob, edits: [{ id: 'r1', source: 'diagram_001.png', output: 'diagram_001__e1.png',
    action: 'retype', target_type: 'realphoto', label: '実写風で作り直し', status: 'ok' }] };
  const p = page(done, { status: 200, data: { adopted: ['diagram_001__e1.png'] } });
  await p.run('pollItems()');
  const mapCard = card(p, 1);
  assert.match(mapCard, /images\/diagram_001__e1\.png/);
  assert.match(mapCard, /実写風（作り直し）/);
  await p.run('toggleCardAdopted(1)');
  assert.deepEqual(p.posts[0].body, { filename: 'diagram_001__e1.png', adopted: true, group: 1 });
  p.run('openModal(1)');
  assert.equal(p.run('modalState.shown'), 'diagram_001__e1.png');
  assert.match(p.element('versionStrip').innerHTML, /元の画像/);  // 地図は元の画像として残る
});

test('拡大画面では、図解だけ同じ種類の再生成も並べる', async () => {
  const p = page(mapJob);
  await p.run('pollItems()');
  p.run('openModal(1)');
  const mapRow = p.element('retypeRow').innerHTML;
  assert.match(mapRow, /地図をやめて作り直す：/);
  assert.match(mapRow, /実写風（おすすめ）/);
  assert.doesNotMatch(mapRow, /'map'\)/);
  p.run('openModal(2)');
  const diagramRow = p.element('retypeRow').innerHTML;
  assert.match(diagramRow, /別の種類で作り直す：/);
  assert.match(diagramRow, /retypeImage\(2, 'map'\)/);
  assert.match(diagramRow, /retypeImage\(2, 'diagram'\)/);
  assert.match(diagramRow, />図解（再生成）</);
  assert.match(diagramRow, /配置や見せ方を変えた別案/);
});

test('図解の再生成を受け付けたらボタンを止め、完了後は元と比較できる', async () => {
  const edit = { id: 'r2', source: 'diagram_002.png', output: 'diagram_002__e1.png', action: 'retype',
    target_type: 'diagram', label: '図解（再生成）', status: 'queued' };
  const p = page(mapJob, { status: 200, data: { edit, edits: [edit] } });
  await p.run('pollItems()');
  p.run('openModal(2)');
  await p.run("retypeImage(2, 'diagram')");
  assert.deepEqual(p.posts[0].body, { source: 'diagram_002.png', action: 'retype', target_type: 'diagram' });
  assert.match(p.element('retypeRow').innerHTML, /disabled[^>]*>図解を作成中…</);
  assert.match(p.element('versionStrip').innerHTML, /図解（再生成）：順番待ち/);
  p.run("editsList[0].status = 'ok'; renderModal();");
  assert.equal(p.run('modalState.shown'), 'diagram_002__e1.png');
  assert.match(p.element('versionStrip').innerHTML, /元の画像/);
  assert.match(p.element('versionStrip').innerHTML, /図解（再生成）/);
  assert.match(p.element('retypeRow').innerHTML, />図解（再生成）</);
});

test('地図から作り直した図解にも再生成を表示する', async () => {
  const p = page({ ...mapJob, edits: [
    { source: 'diagram_001.png', output: 'diagram_001__e1.png', action: 'retype',
      target_type: 'diagram', label: '図解で作り直し', status: 'ok' },
  ] });
  await p.run('pollItems()');
  p.run('openModal(1)');
  assert.match(p.element('retypeRow').innerHTML, />図解（再生成）</);
  p.run("showVersion('diagram_001.png')");
  assert.doesNotMatch(p.element('retypeRow').innerHTML, /button[^>]*>図解（再生成）</);
});
test('受け付けられなかった理由は、そのカードに出す', async () => {
  const p = page(mapJob, { status: 400, data: { error: 'OPENAI_API_KEY が設定されていないため、作り直せません' } });
  await p.run('pollItems()');
  await p.run("retypeImage(1, 'illustration')");
  assert.match(card(p, 1), /role="alert">OPENAI_API_KEY が設定されていないため/);
  assert.doesNotMatch(card(p, 2), /role="alert"/);
});

test('古いジョブ（おすすめの記録なし）でもボタンは出し、おすすめは書かない', async () => {
  const old = { ...mapJob, items: [{ ...mapJob.items[0], map_alt: undefined }, mapJob.items[1]] };
  const p = page(old);
  await p.run('pollItems()');
  const mapCard = card(p, 1);
  assert.match(mapCard, /地図をやめて作り直す</);
  assert.doesNotMatch(mapCard, /おすすめ/);
  assert.doesNotMatch(mapCard, /bg-sky-700/);
});
