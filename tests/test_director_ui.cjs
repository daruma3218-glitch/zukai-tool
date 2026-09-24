const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../templates/progress.html'), 'utf8');
const source = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1].replace('    tick();', '');

function page(itemsPayload, status = 'completed') {
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
      : { status, succeeded: 2, phase: 3, percent: 100 } }),
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
  assert.match(p.element('retentionNote').textContent, /10月24日 ごろに自動で削除/);
});

test('1案のジョブは従来どおりの格子で、採用が無ければ採用ボタンを隠す', async () => {
  const p = page({ available_images: 1, items: [{ index: 1, status: 'ok', filename: 'diagram_001.png' }] });
  await p.run('pollItems()');
  assert.match(p.element('imgGrid').className, /^grid /);
  assert.equal(p.element('adoptedBtn').classList.contains('hidden'), true);
  assert.equal(p.element('adoptHint').classList.contains('hidden'), false);
  assert.equal(p.element('retentionNote').classList.contains('hidden'), true);
});
