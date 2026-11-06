// 入力画面：いつもと違う設定を生成ボタンのそばに出す（2026-11-06）
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const html = fs.readFileSync(process.env.UPLOAD_TEMPLATE || path.join(__dirname, '../templates/upload.html'), 'utf8');
const start = html.indexOf('    function settingsDifferences(');
const end = html.indexOf("    let settingsOrigin = '';");
assert.ok(start > 0 && end > start, 'settingsDifferences が見つからない');
const context = vm.createContext({});
vm.runInContext(html.slice(start, end), context);
const diff = (now, labels) => vm.runInContext('settingsDifferences', context)(now, STD, labels);

const STD = {provider: 'gpt-image', candidate_mode: 'single', openai_quality: 'medium',
  openai_model: 'gpt-image-2.6-flare', map_mode: 'data', worldview_preset: 'roshia',
  target_count: '50', concurrency: '2'};

test('いつもの設定なら何も出さない', () => {
  assert.deepEqual([...diff({...STD})], []);
});

test('図にする箇所・並列は数えない（目安の欄に出ている）', () => {
  assert.deepEqual([...diff({...STD, target_count: '150', concurrency: '1'})], []);
});

test('前回の高い画質・2案は、費用が増えることと一緒に出す', () => {
  const out = [...diff({...STD, openai_quality: 'high', candidate_mode: 'pair'})];
  assert.deepEqual(out, ['候補は2案（画像が約2倍）', '画質 High（費用と時間が増える）']);
});

test('nanobanana の時は画質を数えない', () => {
  const out = [...diff({...STD, provider: 'nanobanana', openai_quality: 'high'})];
  assert.deepEqual(out, ['画像のAIは nanobanana']);
});

test('モデル・地図・世界観の違いを、画面の呼び名で出す', () => {
  const out = [...diff({...STD, openai_model: 'gpt-image-2.6-sunburst', map_mode: 'ai', worldview_preset: ''},
    {openai_model: 'GPT Image 2.6 Sunburst', worldview_preset: 'なし'})];
  assert.deepEqual(out, ['画像のモデルは GPT Image 2.6 Sunburst', '地図を画像AIで描く（地図にも画像代がかかる）',
    '世界観なし（人物・背景・通貨を固定しない）']);
});

test('型違いで2〜3案', () => {
  assert.deepEqual([...diff({...STD, candidate_mode: 'mixed'})], ['候補は型違いで2〜3案（画像が約2〜3倍）']);
});

test('画面に知らせの欄と戻すボタンがあり、戻す時は図にする箇所を変えない', () => {
  assert.match(html, /id="settingsNotice"[^>]*class="hidden/);
  assert.match(html, /id="settingsReset"/);
  assert.match(html, /STANDARD_RESET = Object\.assign\(\{\}, STANDARD_SETTINGS, \{target_count: ''\}\)/);
  assert.match(html, /restoreSettings\(STANDARD_RESET\)/);
  // 前回の設定を戻した後・元のジョブの設定を戻した後に、知らせを更新する
  const restore = html.slice(html.indexOf('    function restoreSettings('), html.indexOf('    function readSettings('));
  assert.match(restore, /updateSettingsNotice\(\);/);
});
