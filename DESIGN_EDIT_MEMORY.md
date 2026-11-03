# 手直しのメモリ保持対策（2026-11-03）

## 根拠と範囲

本番ログでは20:13〜20:21 JSTの手直しの繰り返しでworker RSSが約348MBから478MBへ増え、
20:37ごろに上限512MiBを超えて再起動した。起動後の状態整理対象は手直し2件、ジョブ0件。
Render Eventsの記録時刻21:45:49 JSTと実起動ログの時刻は異なるため区別する。
具体的な保持オブジェクトの本番ヒープは取得しておらず、原因を一つのSDK関数へ断定しない。

通常の参照カウントで解放されないSDK/HTTPのオブジェクトと、画像の処理後にOSへ返らない
ネイティブ領域を処理ごとに回収する。タイマーや強制的なworker再起動で解消する方式は使わない。

## 修正

- OpenAI手直しは公式SDKの `with_streaming_response.edit` をcontext managerで使用。
  HTTPを待っている間は全応答を読み込まず、画像処理枠を取ってからparseする。
  成功・空の応答・破損・タイムアウト・保存失敗でも応答を閉じる。
- 通信を含む画像タスク上限2は維持し、手直しの入力画像準備・応答展開・変換・保存は1枚ずつ。
  通信待ちには画像のネイティブ領域を残さない。
- 画像関数のローカル変数が消えた時点、各試行後、編集クライアントclose後にGCを実行。
  Render Linuxのglibcで利用可能なら `malloc_trim(0)` で未使用領域をOSへ返す。
  元画像・採用記録・設定・ジョブ履歴は回収対象にしない。
- `/version` は `memory_safety.version=3`、`edit_pixel_limit=1`、
  `edit_response_streaming=true` を返す。

モデル・画質・解像度・再試行回数・保存期限・プラン・worker数は変更しない。
手直しは従来どおり別ファイルへ保存し、採用は本人の操作でのみ変更する。

## 検証

`python tools/benchmark_edit_memory.py --count 32 --check-stable` は実SDK・模擬HTTP・
1536×1024の合成PNGで同じworkerプロセスから32回、新しいスレッドとクライアントで手直しする。
外部API・本番データを使わず、一時ディレクトリだけに画像を保存する。

Windows / OpenAI SDK 2.32.0での比較:

| 指標 | 修正前 | 修正後 |
|---|---:|---:|
| RSS最大 | 216,207,360 bytes | 134,377,472 bytes |
| 1枚目→32枚目のRSS増加 | 19,574,784 bytes | 532,480 bytes |
| 同じ区間のPython保持量増加 | 19,016,237 bytes | 25,929 bytes |
| Python割当量最大 | 107,557,022 bytes | 31,732,394 bytes |

全出力のSHA-256は両版とも
`44c83040f3ab2270e14a6a277d90824d14896ec1631d3bc5789f8892820182aa`。
これは本番環境の上限保証ではない。CIでもLinux上の同じ模擬計測と安定性検査を行う。
CIの依存指定はRenderのbuildCommandと同じrequirements-migration.txtへ揃える。

`tests/test_edit_memory.py` は実SDKの模擬通信で、空応答・不正な画像・保存失敗・429・タイムアウト後の
close/再試行、明示GCによる応答解放、2通信と1展開、元画像と採用の不変を確認する。
既存の保存期限試験1件は固定された作成日時と現在日時がずれて古くなるため、その試験の時計だけを固定。
本番の保存期限処理は変更しない。

## 本番反映の条件

既存 `deploy_guard.py` の受付保護を成功させ、queued/runningの手直しと生成0件を確認する。
その状態で保存画像・adoption.json・selection.json・原稿/設定/ジョブJSONの全ファイルを
相対パス・サイズ・SHA-256で記録する。元ファイルは書き換えない。
mainへの反映で1回だけ自動deployし、Live・コミット・version=3・受付再開・保存データ一致を確認する。
検証用の有料画像生成や強制停止は行わない。

## 本番反映・保持確認（2026-11-03 完了）

ブラウザー操作の自動安全確認が現在URLを特定できず停止したため、本人がShellで受付保護と保存データ照合を実行した。
生成・手直し0件と保存データの証拠を受領してから、d6d36069a5cf1ad3d7a9660d9d7262fbee4b2885をmainへ早送り。
Render dep-dbkusebbc2fs738cknt0は23:24:12 JSTにLive。version=3・画像タスク上限2・手直し展開上限1とgateway/workerを確認した。

前後とも3,653ファイル・4,052,190,164 bytes、画像2,811枚、adoption.json 10件、selection.json 6件。
追加・欠損0件。3,652ファイルのSHA-256が一致し、画像・採用・選定・原稿・設定を保持。
差分1件は既存の自動整理が保存するretention_state.json（126 bytes）のみ。本文を前後比較していないため時刻だけの差分とは断定しない。
既存の自動整理処理と保存期間は変更しておらず、この実行状態記録は巻き戻さない。
新instance khsxwでguard/readonlyマーカー不在・MIGRATION_ACCESS=activeを本人が読戻し、受付再開まで確認した。

Renderと同じrequirements-migration.txt / OpenAI SDK3.14.1のLinux CIはPython296件・JS55件と32回の模擬手直しが成功。
Linux模擬計測はRSS最大130,424,832 bytes、32回目124,039,168 bytes。本番起動後のRender値は117,047,300 bytes。
本番の有料画像生成は検証目的で行っておらず、実運用での再発ゼロを保証するものではない。

運用正本はAI_Workspaceのdocs/zukai-edit-memory-release-2026-11-03.md。
