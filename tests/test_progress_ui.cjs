const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../templates/progress.html'), 'utf8');
const source = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1]
  .replace('    tick();', '');

function page(status, images) {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) {
      const classes = new Set(id === 'actions' ? ['hidden'] : []);
      elements.set(id, {
        textContent: '', innerHTML: '', style: {}, href: '',
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
    window: {}, console,
    document: { getElementById: element, addEventListener() {} },
    fetch: async url => ({ ok: true, json: async () => url.includes('/api/items/')
      ? { available_images: images, items: images ? [{ index: 1, status: 'ok', filename: 'diagram_001.png' }] : [] }
      : { status, succeeded: images, phase: 3, percent: status === 'completed' ? 100 : 75 } }),
  });
  vm.runInContext(source, context);
  return { element, run: command => vm.runInContext(command, context) };
}

test('生成途中の保存済み画像を、照合表示なしですぐダウンロードできる', async () => {
  const p = page('running', 1);
  await p.run('pollItems()');
  await p.run('pollStatus()');
  assert.equal(p.element('actions').classList.contains('hidden'), false);
  assert.match(p.element('downloadLabel').textContent, /完成済み 1枚/);
  assert.match(p.element('downloadBtn').href, /^\/download\//);
  assert.doesNotMatch(p.element('imgGrid').innerHTML, /内容検査|未確認|要修正/);
});

test('エラーになっても完成済み画像のダウンロードを維持する', async () => {
  const p = page('error', 1);
  await p.run('pollItems()');
  await p.run('pollStatus()');
  assert.equal(p.element('actions').classList.contains('hidden'), false);
  assert.equal(p.run('stopped'), true);
});

test('完了通知が画像一覧より先に届いても、一覧取得後にダウンロードできる', async () => {
  const p = page('completed', 2);
  await p.run('pollStatus()');
  assert.equal(p.element('actions').classList.contains('hidden'), true);
  await p.run('pollItems()');
  assert.equal(p.element('actions').classList.contains('hidden'), false);
  assert.match(p.element('downloadLabel').textContent, /ZIP でダウンロード（2枚）/);
});

test('保存済み画像がゼロなら空ZIPへのボタンを出さない', async () => {
  const p = page('completed', 0);
  await p.run('pollStatus()');
  await p.run('pollItems()');
  assert.equal(p.element('actions').classList.contains('hidden'), true);
});
