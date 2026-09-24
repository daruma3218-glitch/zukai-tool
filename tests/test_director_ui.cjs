const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../templates/progress.html'), 'utf8');
const source = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1].replace('    tick();', '');

function page(itemsPayload, status = 'completed', statusPayload = null) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) {
      const classes = new Set(['actions', 'adoptedBtn', 'adoptHint', 'retentionNote'].includes(id) ? ['hidden'] : []);
      elements.set(id, {
        textContent: '', innerHTML: '', style: {}, href: '', className: '',
        classList: {
          toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
          contains: name => classes.has(name),
        },
        querySelectorAll: () => [],
      });
    }
    return elements.get(id);
  };
  const context = vm.createContext({
    window: {}, console, setTimeout: () => 0,
    document: { getElementById: element, addEventListener() {} },
    fetch: async url => ({ ok: true, json: async () => url.includes('/api/items/')
      ? itemsPayload
      : (statusPayload || { status, succeeded: 2, phase: 3, percent: 100 }) }),
  });
  vm.runInContext(source, context);
  return { element, run: command => vm.runInContext(command, context) };
}

const candidates = {
  available_images: 2,
  items: [
    { index: 1, group: 1, variant: 'a', variant_label: '図解', status: 'ok', filename: 'diagram_001_a.png', excerpt: '抜粋A' },
    { index: 2, group: 1, variant: 'b', variant_label: '地図', status: 'ok', filename: 'diagram_001_b.png', excerpt: '抜粋A' },
  ],
  adopted: ['diagram_001_b.png'],
  edits: [{ id: 'e', source: 'diagram_001_a.png', output: 'diagram_001_a__e1.png', status: 'running', label: '文字を全部消す' }],
  retention: { days: 30, expires_at: '2026-10-24T10:00' },
};

test('候補は同じ抜粋の案を横に並べ、案の番号と種類を出す', async () => {
  const p = page(candidates);
  await p.run('pollItems()');
  const grid = p.element('imgGrid');
  assert.equal(grid.className, 'space-y-3');
  assert.match(grid.innerHTML, /No\.001/);
  assert.match(grid.innerHTML, /001a/);
  assert.match(grid.innerHTML, /001b/);
  assert.match(grid.innerHTML, /地図/);
});

test('採用した画像があると「採用分だけZIP」を出す', async () => {
  const p = page(candidates);
  await p.run('pollItems()');
  assert.equal(p.element('adoptedBtn').classList.contains('hidden'), false);
  assert.match(p.element('adoptedLabel').textContent, /採用分だけZIP（1枚）/);
  assert.match(p.element('adoptedBtn').href, /^\/download-adopted\//);
  assert.match(p.element('imgGrid').innerHTML, /★/);
});

test('手直し中の画像に印を出し、削除予定日を知らせる', async () => {
  const p = page(candidates);
  await p.run('pollItems()');
  assert.match(p.element('imgGrid').innerHTML, /直し中/);
  assert.equal(p.element('retentionNote').classList.contains('hidden'), false);
  assert.match(p.element('retentionNote').textContent, /10月24日 ごろに、採用していない画像が自動で削除/);
  assert.match(p.element('retentionNote').textContent, /採用した画像（1枚）は残ります/);
});

test('採用が無いときは、残したい画像を☆で採用するよう知らせる', async () => {
  const p = page({ ...candidates, adopted: [], edits: [] });
  await p.run('pollItems()');
  assert.match(p.element('retentionNote').textContent, /10月24日 ごろに自動で削除/);
  assert.match(p.element('retentionNote').textContent, /☆で採用/);
});

test('保存期限の整理後は、採用画像だけを残したと知らせる', async () => {
  const p = page({ ...candidates, retention: { days: 30, expires_at: null, trimmed: true } });
  await p.run('pollItems()');
  assert.equal(p.element('retentionNote').classList.contains('hidden'), false);
  assert.match(p.element('retentionNote').textContent, /採用した画像だけを残しています/);
});

test('1案のジョブは従来どおりの格子で、採用が無ければ採用ボタンを隠す', async () => {
  const p = page({ available_images: 1, items: [{ index: 1, status: 'ok', filename: 'diagram_001.png' }] });
  await p.run('pollItems()');
  assert.match(p.element('imgGrid').className, /^grid /);
  assert.equal(p.element('adoptedBtn').classList.contains('hidden'), true);
  assert.equal(p.element('adoptHint').classList.contains('hidden'), false);
  assert.equal(p.element('retentionNote').classList.contains('hidden'), true);
});


test('4つの段階: 抽出中は②が進行中で①は完了、経過時間も出す', async () => {
  const p = page(candidates, 'running', { status: 'running', phase: 1, percent: 12,
    message: '視覚化ポイントを 10 個抽出中...', elapsed_seconds: 75 });
  await p.run('pollStatus()');
  assert.match(p.element('step1').className, /green/);
  assert.match(p.element('step2').className, /purple/);
  assert.match(p.element('step3').className, /gray/);
  assert.equal(p.element('elapsedNote').textContent, '経過 1分15秒');
});

test('4つの段階: 完了で全部済み、止まった時はその段階を赤で示す', async () => {
  const done = page(candidates, 'completed', { status: 'completed', phase: 3, percent: 100, elapsed_seconds: 575 });
  await done.run('pollStatus()');
  [1, 2, 3, 4].forEach(n => assert.match(done.element(`step${n}`).className, /green/));
  assert.equal(done.element('elapsedNote').textContent, '所要 9分35秒');
  const failed = page(candidates, 'error', { status: 'error', phase: 2, percent: 30, elapsed_seconds: 200 });
  await failed.run('pollStatus()');
  assert.match(failed.element('step3').className, /red/);
});

test('採用の内訳と、番号の開始つきの採用分ZIP', async () => {
  const p = page({ ...candidates, adopted: ['diagram_001_b.png', 'diagram_001_a__e1.png'] });
  await p.run('pollItems()');
  assert.equal(p.element('adoptTools').classList.contains('hidden'), false);
  assert.equal(p.element('adoptSummary').textContent,
    '採用 2枚（地図 1・図解 1）／ 採用が決まった箇所 1 / 1');
  assert.match(p.element('adoptedBtn').href, /\?start=1$/);
  p.element('adoptStart').value = '105';
  await p.run('updateDownload()');
  assert.match(p.element('adoptedBtn').href, /\?start=105$/);
});
