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
  assert.doesNotMatch(p.element('imgGrid').innerHTML, /内容検査|未確認/);
  assert.match(p.element('imgGrid').innerHTML, /要修正にする/); // 任意の手動印。DLの条件にはしない
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
  assert.match(p.element('downloadLabel').textContent, /全候補を保存（2枚・未採用も含む）/);
});

test('保存済み画像がゼロなら空ZIPへのボタンを出さない', async () => {
  const p = page('completed', 0);
  await p.run('pollStatus()');
  await p.run('pollItems()');
  assert.equal(p.element('actions').classList.contains('hidden'), true);
});

test('同じ箇所の別案は採用を求めず、全箇所を選ぶと保存へ案内する', () => {
  const p = page('completed', 3);
  p.run(`knownItems = {
    1:{index:1,group:1,status:'ok',filename:'diagram_001_a.png'},
    2:{index:2,group:1,status:'ok',filename:'diagram_001_b.png'},
    3:{index:3,group:2,status:'ok',filename:'diagram_002.png'}
  }; jobStatus='completed'; adoptedSet=new Set(['diagram_001_a.png']); updateReviewGuide();`);
  assert.equal(p.element('reviewNextBtn').textContent, '未採用の画像を確認 →');
  p.run(`adoptedSet.add('diagram_002.png'); updateReviewGuide();`);
  assert.match(p.element('reviewGuide').textContent, /採用画像を保存/);
  assert.equal(p.element('adoptedBtn').style.background, '#6b21a8');
  p.run(`revisionSet.add('diagram_002.png'); updateReviewGuide();`);
  assert.equal(p.element('reviewNextBtn').textContent, '要修正の画像を確認 →');
  assert.equal(p.element('adoptedBtn').style.background, '');
});
