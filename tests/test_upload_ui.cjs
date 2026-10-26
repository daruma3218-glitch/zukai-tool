const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const html = fs.readFileSync(process.env.UPLOAD_TEMPLATE || path.join(__dirname, '../templates/upload.html'), 'utf8');
const source = html.slice(html.indexOf("    const dropZone ="), html.indexOf('    // ハブの案件から'));
const recovery = html.slice(html.indexOf('    async function restoreJobSettings()'), html.indexOf('    // フォーム送信'))
  .replace('    restoreJobSettings();', '    window.ready = restoreJobSettings();');

function page(search = '') {
  const nodes = new Map(), calls = [];
  let response = {ok: false, data: {error: '読み込みに失敗しました。'}};
  let focused = '';
  const node = id => {
    if (!nodes.has(id)) {
      const classes = new Set(['hidden']); let value = '';
      const el = {files: [], disabled: false, textContent: '', handlers: {},
        classList: {add: k => classes.add(k), remove: k => classes.delete(k),
          toggle: (k,on) => on ? classes.add(k) : classes.delete(k), contains: k => classes.has(k)},
        addEventListener: (event, fn) => {el.handlers[event] = fn;},
        focus: () => {focused = id;}, click: () => el.handlers.click?.({preventDefault(){}})};
      Object.defineProperty(el, 'value', {get: () => value, set: v => {value = v; if (id === 'fileInput' && !v) el.files = [];}});
      nodes.set(id, el);
    }
    return nodes.get(id);
  };
  const location = {href: 'https://test.invalid/' + search, search};
  const context = vm.createContext({console, URL, URLSearchParams,
    window: {location, history: {replaceState: (_a,_b,url) => {location.href = 'https://test.invalid' + url; location.search = new URL(location.href).search;}}},
    document: {getElementById: node},
    userInstructions: node('userInstructions'), restoreSettings: () => {},
    fetch: async (url, options) => {calls.push({url,options}); return {ok: response.ok, json: async () => response.data};}});
  vm.runInContext(source + recovery, context);
  return {node, calls, ready: context.window.ready, run: s => vm.runInContext(s,context),
    reply: v => {response = v;}, click: id => node(id).click(), focused: () => focused, location};
}

test('100文字で生成可能になり、ファイルを外すと本文へ戻る', async () => {
  const p = page(); await p.ready;
  p.node('manuscriptText').value = 'あ'.repeat(99); p.run('updateInputReadiness()');
  assert.equal(p.node('submitBtn').disabled, true);
  p.node('manuscriptText').value += 'あ'; p.run('updateInputReadiness()');
  assert.equal(p.node('submitBtn').disabled, false);
  p.node('fileInput').files = [{name:'完成原稿.txt'}]; p.node('fileInput').handlers.change();
  assert.match(p.node('generation-readiness').textContent, /ファイル/);
  assert.equal(p.node('fileSelection').classList.contains('hidden'), false);
  p.click('clearFile');
  assert.equal(p.node('fileInput').files.length, 0);
  assert.equal(p.node('manuscriptText').value.length, 100);
  assert.match(p.node('generation-readiness').textContent, /本文/);
  assert.equal(p.focused(), 'manuscriptText');
  p.node('manuscriptText').value = ''; p.run('updateInputReadiness()');
  assert.equal(p.node('submitBtn').disabled, true);
});

test('読み込み失敗から手入力へ戻っても原稿と他のURL項目を保つ', async () => {
  const p = page('?retry=old-job&card=keep-card'); await p.ready;
  assert.equal(p.node('submitBtn').disabled, true);
  assert.equal(p.node('restoreJobActions').classList.contains('hidden'), false);
  p.node('manuscriptText').value = '残す原稿'.repeat(30);
  p.node('userInstructions').value = 'この指示も残す';
  p.click('continueManually');
  assert.equal(p.node('manuscriptText').value, '残す原稿'.repeat(30));
  assert.equal(p.node('userInstructions').value, 'この指示も残す');
  assert.equal(p.location.search, '?card=keep-card');
  assert.equal(p.node('submitBtn').disabled, false);
  assert.equal(p.node('restoreJobActions').classList.contains('hidden'), true);
  assert.equal(p.calls.length, 1);
  assert.ok(p.calls.every(c => !c.options?.method));
});

test('再読み込みが成功すると復元し、生成は自動で開始しない', async () => {
  const p = page('?retry=old-job'); await p.ready;
  p.reply({ok:true,data:{manuscript_text:'復元する原稿'.repeat(30),settings:{user_instructions:'水彩で'}}});
  await p.click('retryRestore');
  assert.equal(p.node('manuscriptText').value, '復元する原稿'.repeat(30));
  assert.equal(p.node('userInstructions').value, '水彩で');
  assert.equal(p.node('submitBtn').disabled, false);
  assert.equal(p.node('restoreJobActions').classList.contains('hidden'), true);
  assert.equal(p.calls.length, 2);
  assert.ok(p.calls.every(c => !c.options?.method));
});
