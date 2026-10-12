const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../templates/progress.html'), 'utf8');
const source = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1].replace('    tick();', '');

function page(status = 'running') {
  const nodes = new Map(), calls = [];
  let reply = { status };
  const element = id => {
    if (!nodes.has(id)) {
      const classes = new Set(['hidden']);
      nodes.set(id, { textContent: '', innerHTML: '', style: {}, href: '', disabled: false,
        classList: { toggle: (k, on) => on ? classes.add(k) : classes.delete(k), contains: k => classes.has(k) },
        querySelectorAll: () => [], focus() {},
      });
    }
    return nodes.get(id);
  };
  const context = vm.createContext({
    console, setTimeout: () => 0, window: {},
    document: { getElementById: element, addEventListener() {} },
    fetch: async (url, options) => {
      if (url.includes('/api/cancel/')) {
        calls.push({ url, options });
        return { ok: true, json: async () => reply };
      }
      return { ok: true, json: async () => url.includes('/api/items/')
        ? { available_images: 1, items: [{index: 1, status: 'ok', filename: 'diagram_001.png'}] }
        : { status, elapsed_seconds: 12, percent: 40 } };
    },
  });
  vm.runInContext(source, context);
  return { element, calls, run: s => vm.runInContext(s, context),
    status: s => { status = s; }, reply: r => { reply = r; } };
}

test('中止処理中は二重送信を防ぎ、ダウンロードを維持する', async () => {
  const p = page('cancelling');
  await p.run('pollStatus();'); await p.run('pollItems();');
  assert.equal(p.element('cancelBtn').disabled, true);
  assert.match(p.element('statusBadge').innerHTML, /中止処理中/);
  assert.equal(p.run('stopped'), false);
  assert.equal(p.element('actions').classList.contains('hidden'), false);
  await p.run('cancelJob()');
  assert.equal(p.calls.length, 0);
});

test('画像ゼロの中止でも作り直しの入口があり、完了通知を出さない', async () => {
  const p = page('cancelled');
  await p.run('pollStatus()');
  assert.equal(p.run('stopped'), true);
  assert.equal(p.element('cancelBtn').classList.contains('hidden'), true);
  assert.equal(p.element('restartBtn').classList.contains('hidden'), false);
  assert.match(p.element('elapsedNote').textContent, /停止まで/);
  assert.match(p.element('statusBadge').innerHTML, /中止済み/);
  assert.match(p.run("renderItem({index: 2, status: 'cancelled'})"), /中止（未生成）/);
});

test('中止の送信成功後に古い実行中ポーリングが届いても逆戻りしない', async () => {
  const p = page('running');
  p.reply({ status: 'cancelling' });
  p.run('showCancelConfirmation()');
  await p.run('cancelJob()');
  assert.equal(p.run('jobStatus'), 'cancelling');
  assert.equal(p.element('cancelBtn').disabled, true);
  assert.equal(p.calls.length, 1);
  assert.equal(p.calls[0].options.method, 'POST');
  assert.ok(p.calls[0].options.headers['X-CSRF-Token']);
});

test('送信に失敗したら中止済みにせず再試行できる', async () => {
  const p = page('running');
  p.reply({ error: '通信失敗' });
  p.run('showCancelConfirmation()');
  await p.run('cancelJob()');
  assert.equal(p.run('jobStatus'), 'running');
  assert.equal(p.element('cancelBtn').disabled, false);
  assert.equal(p.element('cancelError').classList.contains('hidden'), false);
});

test('中止済みの後から届いた古い中止処理中の応答を無視する', async () => {
  const p = page('cancelled');
  await p.run('pollStatus()');
  p.status('cancelling');
  await p.run('pollStatus()');
  assert.equal(p.run('jobStatus'), 'cancelled');
  assert.match(p.element('statusBadge').innerHTML, /中止済み/);
});

test('確認を取り消したらAPIを呼ばない', async () => {
  const p = page('running');
  p.run('showCancelConfirmation()');
  assert.equal(p.element('cancelConfirmation').classList.contains('hidden'), false);
  assert.equal(p.calls.length, 0);
  p.run('hideCancelConfirmation()');
  await p.run('cancelJob()');
  assert.equal(p.calls.length, 0);
});

test('確認中に完了した場合は中止を送らない', async () => {
  const p = page('running');
  p.run('showCancelConfirmation()');
  p.status('completed');
  await p.run('pollStatus()');
  await p.run('cancelJob()');
  assert.equal(p.element('cancelConfirmation').classList.contains('hidden'), true);
  assert.equal(p.calls.length, 0);
});
