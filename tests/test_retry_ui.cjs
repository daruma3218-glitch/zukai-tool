const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const html = fs.readFileSync(path.join(__dirname, '../templates/progress.html'), 'utf8');
const source = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1].replace('    tick();', '');

function page() {
  const nodes = new Map(), calls = [];
  let status = 'cancelled', wait = {show: false}, reloaded = 0;
  let plan = {enabled: true, count: 2, kept: 1, ai_images: 2, maps: 0,
    model: 'gpt-image-2', quality: 'high', concurrency: 1, plan_token: 'saved-plan'};
  let post = {ok: true, data: {run_id: 'attempt-2'}};
  const element = id => {
    if (!nodes.has(id)) {
      const classes = new Set(['hidden']);
      nodes.set(id, {textContent: '', innerHTML: '', style: {}, disabled: false,
        classList: {toggle: (k,on) => on ? classes.add(k) : classes.delete(k), contains: k => classes.has(k)},
        querySelectorAll: () => [], focus() {}});
    }
    return nodes.get(id);
  };
  const context = vm.createContext({console, setTimeout: () => 0,
    window: {location: {reload: () => {reloaded++;}}},
    document: {getElementById: element, addEventListener() {}},
    fetch: async (url, options) => {
      calls.push({url, options});
      if (url.includes('/api/retry-missing/')) {
        if (options?.method === 'POST') return {ok: post.ok, json: async () => post.data};
        return {ok: true, json: async () => plan};
      }
      return {ok: true, json: async () => ({status, wait, elapsed_seconds: 600})};
    }});
  vm.runInContext(source, context);
  return {element, calls, run: s => vm.runInContext(s, context),
    plan: p => {plan = p;}, post: p => {post = p;}, status: s => {status = s;}, wait: w => {wait = w;},
    reloaded: () => reloaded, posts: () => calls.filter(c => c.options?.method === 'POST')};
}

test('対象枚数・完成済み・モデル・追加料金を確認してから再生成する', async () => {
  const p = page();
  await p.run('pollStatus()');
  assert.equal(p.element('retryMissingBtn').classList.contains('hidden'), false);
  await p.run('showRetryConfirmation()');
  assert.match(p.element('retrySummary').textContent, /再生成 2枚.*完成済み 1枚/);
  assert.match(p.element('retrySettings').textContent, /gpt-image-2.*画質：高/);
  assert.match(p.element('retryCost').textContent, /追加/);
  assert.equal(p.posts().length, 0);
  await p.run('retryMissingImages()');
  assert.equal(p.posts().length, 1);
  assert.equal(JSON.parse(p.posts()[0].options.body).plan_token, 'saved-plan');
  assert.ok(p.posts()[0].options.headers['X-CSRF-Token']);
  assert.equal(p.reloaded(), 1);
  await p.run('retryMissingImages()');
  assert.equal(p.posts().length, 1);
});

test('やめるを選ぶと生成しない', async () => {
  const p = page();
  await p.run('pollStatus();');
  await p.run('showRetryConfirmation()');
  p.run('hideRetryConfirmation()');
  await p.run('retryMissingImages()');
  assert.equal(p.posts().length, 0);
});

test('対象変更・通信失敗の後は再確認が必要で自動再送しない', async () => {
  const p = page();
  await p.run('pollStatus()');
  await p.run('showRetryConfirmation()');
  p.post({ok: false, data: {error: '対象が変わりました'}});
  await p.run('retryMissingImages()');
  assert.match(p.element('retryError').textContent, /対象が変わりました/);
  assert.equal(p.element('retryConfirmation').classList.contains('hidden'), true);
  assert.equal(p.reloaded(), 0);
  await p.run('retryMissingImages()');
  assert.equal(p.posts().length, 1);
  assert.equal(p.element('retryMissingBtn').disabled, false);
});

test('全画像がある場合は再生成を出さない', async () => {
  const p = page();
  p.plan({enabled: false, code: 'nothing_missing', reason: 'すべて保存済み'});
  await p.run('pollStatus()');
  assert.equal(p.element('retryMissingBtn').classList.contains('hidden'), true);
  assert.equal(p.element('retryReason').classList.contains('hidden'), true);
});

test('設計前に中止した場合は作り直しを案内する', async () => {
  const p = page();
  p.plan({enabled: false, code: 'no_plan', reason: '設定を直して作り直してください'});
  await p.run('pollStatus()');
  assert.equal(p.element('retryReason').classList.contains('hidden'), false);
  assert.equal(p.element('restartBtn').classList.contains('hidden'), false);
  assert.equal(p.posts().length, 0);
});

test('終了直後にバックグラウンド処理が残っても再確認の入口を維持する', async () => {
  const p = page();
  p.plan({enabled: false, code: 'active', reason: '実行中のジョブです'});
  await p.run('pollStatus()');
  assert.equal(p.element('retryMissingBtn').classList.contains('hidden'), false);
  await p.run('showRetryConfirmation()');
  assert.equal(p.element('retryConfirmation').classList.contains('hidden'), true);
  assert.equal(p.posts().length, 0);
});

test('地図だけの再生成では画像AI料金なしと表示する', async () => {
  const p = page();
  p.plan({enabled: true, count: 1, kept: 2, ai_images: 0, maps: 1,
    model: '地図データ', quality: null, concurrency: 1, plan_token: 'map-plan'});
  await p.run('pollStatus()');
  await p.run('showRetryConfirmation()');
  assert.match(p.element('retryCost').textContent, /生成料金はかかりません/);
});

test('待機案内は進捗回復・終了時に消え、中止や再生成を送らない', async () => {
  const p = page();
  p.status('running');
  p.wait({show: true, seconds_since_progress: 240, stage: '候補の設計', message: '処理用PCとの接続を確認できません。'});
  await p.run('pollStatus()');
  assert.equal(p.element('waitNotice').classList.contains('hidden'), false);
  assert.match(p.element('waitTitle').textContent, /4分以上/);
  assert.match(p.element('waitMessage').textContent, /接続を確認できません/);
  assert.equal(p.posts().length, 0);
  p.wait({show: false});
  await p.run('pollStatus()');
  assert.equal(p.element('waitNotice').classList.contains('hidden'), true);
  p.status('cancelled');
  p.wait({show: true});
  await p.run('pollStatus()');
  assert.equal(p.element('waitNotice').classList.contains('hidden'), true);
});
