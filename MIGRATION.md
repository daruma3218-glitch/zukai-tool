# 図解つくーる移設準備（まだ本番反映しない）

旧本番 b009e06 から分岐。旧側は一時ディスクに画像を保存しているため、
**旧mainへマージ・push・再配置せず、最新のバックアップを先に取得する。**

移転と運用統一を先行し、1台への統合は後続の負荷検証に分ける。
新側の候補は Starter $7 + 5GB $1.25 = 月額基本 $8.25。
既存バックアップは約0.765GB。新設課金はまだ未承認、サービスも未作成。

準備したもの:

- 移行中は既定で変更受付停止、認証・/data必須の `migration_entry:create_app()`。
- 生成完了を待って配置する `deploy_guard.py`。新側の検証・本番切替が済んでから有効化する。
- 旧側で確認したSDK版を固定する `requirements-migration.txt`。
  直前確認で版が変わっていた場合は旧実測値と照合し直す。

初回設定案:

```
Build: pip install -r requirements-migration.txt
Start: gunicorn 'migration_entry:create_app()' --bind 0.0.0.0:$PORT --timeout 600 --workers 1 --threads 8 --worker-tmp-dir /dev/shm
DATA_DIR=/data
MIGRATION_ACCESS=readonly
```

旧側のAPP_PASSWORD、SECRET_KEY、共通画像API・Supabase等をこのツールだけに保持。
センテンスのチャンネル別キーへ混ぜない。旧と同じPython 3.11系を直前実測で確定する。
9/23バックアップは `data/` 直下がoutput内容なので、新側 `/data/output` へ対応させる。
新側の完成画面/画像/ZIPと再起動後の保持、小規模の実生成を確認し、
旧側の受付停止・全件差分照合後に新側だけactiveにする。

最新データと環境値の確認、費用承認、作成・実生成・切替は未実施。
ここでのテスト合格は本番移行の完了ではない。
