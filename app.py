#!/usr/bin/env python3
"""図解つくーる - Flask Web アプリケーション

シンプルな1機能アプリ:
  原稿アップロード → N 枚の図解画像を並列生成 → ZIP ダウンロード
"""

import functools
import json
import os
import secrets
import tempfile
import threading
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)

from utils import load_env, load_json, save_json
from pipeline import DiagramPipeline
from generator import PROVIDER_NANOBANANA, PROVIDER_GPT_IMAGE, VALID_PROVIDERS


PROJECT_ROOT = Path(__file__).parent


def _resolve_output_root() -> Path:
    """生成物の保存先ルート。Render Disk があればそちらを使う（再起動しても消えない）。

    優先順: DATA_DIR 環境変数 → /data（Render Disk の標準マウント先）→ プロジェクト直下。
    ディスク未接続の環境（ローカル等）では従来どおり動く。
    """
    env_dir = os.environ.get("DATA_DIR", "").strip()
    if env_dir:
        p = Path(env_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p
    p = Path("/data")
    if p.exists():
        return p
    return PROJECT_ROOT


OUTPUT_DIR = _resolve_output_root() / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# .env をロード
load_env(PROJECT_ROOT)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
# 放置してもログアウトされないように（家事等の合間の運用向け）。
# ※Render 側で SECRET_KEY を設定しないと、再起動時に全セッションが無効化される点に注意
app.permanent_session_lifetime = timedelta(hours=12)

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

# ジョブ状態（メモリ）
_jobs: dict[str, dict] = {}
_job_logs: dict[str, list] = {}
_jobs_lock = threading.Lock()


# ====== 認証 ======
def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not APP_PASSWORD:
            return f(*args, **kwargs)
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/settings/api-usage")
@app.route("/api/key-attribution")
@login_required
def key_attribution():
    """ログイン後の課金先診断。キー本体は返さない。"""
    if not APP_PASSWORD:
        return jsonify({"error": "診断にはアプリのログイン設定が必要です。"}), 403
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    data = {
        "service": "zukai-tool", "provider": "gemini", "configured": bool(key),
        "key_suffix": key[-4:] if len(key) > 4 else "",
        "source_env": "GEMINI_API_KEY" if key else None,
    }
    if request.path == "/settings/api-usage":
        response = make_response(render_template("api_usage.html", audit=data))
    else:
        response = jsonify(data)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/version")
def version():
    """Render が実際にどの版を動かしているかの軽量診断（認証不要・秘密情報なし）。"""
    from extractor import CLAUDE_MODEL as EXTRACTOR_MODEL
    from prompter import CLAUDE_MODEL as PROMPTER_MODEL
    import subscription_runtime
    return jsonify({
        "service": "zukai-tool",
        "git_commit": os.environ.get("RENDER_GIT_COMMIT", ""),
        "routing_version": subscription_runtime.ROUTING_VERSION,
        "editorial_models": {"selection": EXTRACTOR_MODEL, "design": PROMPTER_MODEL},
        "image_review_enabled": False,
        "pipeline_phases": 3,
        "partial_download_enabled": True,
        "llm_billing": "subscription_cli_only", "llm_api_fallback": False,
    })


@app.route("/api/subsk-health")
def api_subsk_health():
    """サブスクLLMゲートウェイの状態確認 (2026-08-13・shiryou-tool と同型)。真偽値のみ。

    gateway_enabled = SUPABASE_URL/KEY が設定済みか (Render環境変数の反映確認)
    worker_alive    = 社長PCのワーカーのハートビートが60秒以内か (転送路の生存確認)
    """
    try:
        from subsk_gateway import _conf, _worker_alive
        import subscription_runtime
        enabled = _conf() is not None
        return jsonify({
            "gateway_enabled": enabled,
            "worker_alive": _worker_alive() if enabled else False,
            "routing_version": subscription_runtime.ROUTING_VERSION,
            "llm_billing": "subscription_cli_only", "llm_api_fallback": False,
        })
    except Exception as e:
        return jsonify({"gateway_enabled": False, "worker_alive": False,
                        "error": type(e).__name__})


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect(url_for("index"))
    if session.get("authenticated"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if request.form.get("password", "") == APP_PASSWORD:
            session.permanent = True  # 12時間持続（ブラウザを閉じても維持）
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "パスワードが正しくありません"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ====== ジョブ管理 ======
@app.after_request
def prevent_stale_progress(response):
    if request.path.startswith(("/api/", "/progress/")) or request.path == "/version":
        response.headers["Cache-Control"] = "no-store"
    return response


def _set_job_state(job_id: str, **kwargs):
    with _jobs_lock:
        state = _jobs.setdefault(job_id, {})
        state.update(kwargs)
        state["updated_at"] = datetime.now().isoformat()
        # ファイルにも保存
        try:
            save_json(OUTPUT_DIR / job_id / "job.json", state)
        except Exception:
            pass


def _get_job_state(job_id: str) -> dict:
    # 複数のGunicornワーカーと再起動後も、保存済みの最新状態を優先する。
    state = load_json(OUTPUT_DIR / job_id / "job.json", {})
    if state:
        return state
    with _jobs_lock:
        if job_id in _jobs:
            return dict(_jobs[job_id])
    return {}


def _add_log(job_id: str, category: str, message: str, detail: str = ""):
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "category": category,
        "message": message,
        "detail": detail,
    }
    with _jobs_lock:
        logs = _job_logs.setdefault(job_id, [])
        logs.append(entry)
        try:
            save_json(OUTPUT_DIR / job_id / "logs.json", logs)
        except OSError:
            pass


def _completed_images(result_dir: Path) -> list[Path]:
    """保存が完了した画像だけを列挙する。書き込み途中の.tmpは含めない。"""
    images_dir = result_dir / "images"
    if not images_dir.is_dir():
        return []
    return sorted(path for path in images_dir.iterdir()
                  if path.is_file() and not path.is_symlink()
                  and path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
                  and path.stat().st_size > 0)


def _image_snapshot(result_dir: Path, images=None) -> dict:
    """状態保存が途切れた旧ジョブも、残っている画像から表示・DLを復元する。"""
    images = _completed_images(result_dir) if images is None else images
    snapshot = load_json(result_dir / "images_progress.json", {})
    items = snapshot.get("items") or load_json(result_dir / "manifest.json", {}).get("items") or []
    if not items:
        items = load_json(result_dir / "prompts.json", {}).get("items", [])
    items = [dict(item) for item in items]
    available = {path.name for path in images}
    for item in items:
        if item.get("filename") in available:
            item["status"] = "ok"
        elif item.get("status") == "ok" or item.get("success"):
            item["status"] = "failed"
    by_index = {item.get("index"): item for item in items}
    known = {item.get("filename") for item in items}
    for path in images:
        if path.name in known:
            continue
        suffix = path.stem.rsplit("_", 1)[-1]
        idx = int(suffix) if suffix.isdigit() else max(by_index, default=0) + 1
        item = by_index.get(idx)
        if item is None:
            item = {"index": idx}
            items.append(item)
            by_index[idx] = item
        item.update(filename=path.name, status="ok", success=True)
    snapshot["items"] = sorted(items, key=lambda item: item.get("index", 0))
    snapshot["available_images"] = len(images)
    return snapshot


def _run_pipeline_thread(job_id: str, manuscript_text: str, target_count: int,
                         user_instructions: str, concurrency: int,
                         provider: str = PROVIDER_NANOBANANA,
                         openai_quality: str = "medium",
                         worldview_preset: str = "",
                         no_text_mode: bool = False):
    job_dir = OUTPUT_DIR / job_id
    provider_label = "nanobanana (Gemini)" if provider == PROVIDER_NANOBANANA else f"gpt-image (OpenAI / {openai_quality})"
    try:
        _set_job_state(job_id, status="running", phase=0, message="開始しています...", percent=0)
        _add_log(job_id, "system",
                 f"ジョブ {job_id} を開始（{provider_label} / 目標 {target_count} 枚 / 並列 {concurrency}）")

        def on_progress(phase, msg, pct):
            _set_job_state(job_id, status="running", phase=phase, message=msg, percent=pct)

        def on_log(category, message, detail=""):
            _add_log(job_id, category, message, detail)

        def on_item(info):
            # 個別画像の進捗は images_progress.json 経由でフロントへ
            pass

        pipeline = DiagramPipeline(
            manuscript_text=manuscript_text,
            output_dir=job_dir,
            target_count=target_count,
            user_instructions=user_instructions,
            worldview_preset=worldview_preset,
            no_text_mode=no_text_mode,
            concurrency=concurrency,
            provider=provider,
            openai_quality=openai_quality,
            progress_callback=on_progress,
            log_callback=on_log,
            item_callback=on_item,
        )
        manifest = pipeline.run()
        _set_job_state(
            job_id,
            status="completed",
            phase=3,
            message=f"完了: 成功 {manifest['succeeded']} / {manifest['target_count']} 枚",
            percent=100,
            title=manifest.get("title", ""),
            succeeded=manifest.get("succeeded", 0),
            failed=manifest.get("failed", 0),
            target_count=manifest.get("target_count", 0),
        )
        _add_log(job_id, "system", f"全フェーズ完了（成功 {manifest['succeeded']} / 失敗 {manifest['failed']}）")
    except Exception as e:
        import traceback
        traceback.print_exc()
        _set_job_state(job_id, status="error", message=str(e)[:200], percent=0)
        _add_log(job_id, "error", "パイプライン実行エラー", str(e)[:300])


# ====== ルート ======
@app.route("/")
@login_required
def index():
    # 過去ジョブ一覧
    past_jobs = []
    if OUTPUT_DIR.exists():
        for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            manifest = load_json(d / "manifest.json", {})
            job_state = load_json(d / "job.json", {})
            if not manifest and not job_state:
                continue
            past_jobs.append({
                "id": d.name,
                "title": manifest.get("title", job_state.get("title", d.name)),
                "status": job_state.get("status", "unknown"),
                "succeeded": manifest.get("succeeded", job_state.get("succeeded", 0)),
                "target": manifest.get("target_count", job_state.get("target_count", 0)),
                "date": d.name[:8] if len(d.name) >= 8 else "",
            })
    resp = make_response(render_template(
        "upload.html",
        past_jobs=past_jobs[:30],
        has_anthropic=bool(os.environ.get("ANTHROPIC_API_KEY")),
        has_gemini=bool(os.environ.get("GEMINI_API_KEY")),
        has_openai=bool(os.environ.get("OPENAI_API_KEY")),
    ))
    # デプロイ後に古いフォーム（新しい入力欄が無い）が使われ続けるのを防ぐ
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/start", methods=["POST"])
@login_required
def start_job():
    # provider を先に取得
    provider = request.form.get("provider", PROVIDER_NANOBANANA)
    if provider not in VALID_PROVIDERS:
        provider = PROVIDER_NANOBANANA
    openai_quality = request.form.get("openai_quality", "medium")
    if openai_quality not in ("low", "medium", "high"):
        openai_quality = "medium"

    # API キー確認（プロバイダ別）
    missing = []
    pass  # Subscription CLI does not require an Anthropic API key.
    if provider == PROVIDER_NANOBANANA and not os.environ.get("GEMINI_API_KEY"):
        missing.append("GEMINI_API_KEY")
    if provider == PROVIDER_GPT_IMAGE and not os.environ.get("OPENAI_API_KEY"):
        missing.append("OPENAI_API_KEY")
    if missing:
        return jsonify({"error": f"{', '.join(missing)} が設定されていません"}), 400

    # 原稿取得
    manuscript_text = ""
    if "manuscript_file" in request.files and request.files["manuscript_file"].filename:
        manuscript_text = request.files["manuscript_file"].read().decode("utf-8", errors="ignore")
    elif request.form.get("manuscript_text"):
        manuscript_text = request.form["manuscript_text"]
    else:
        return jsonify({"error": "原稿が入力されていません"}), 400

    if len(manuscript_text.strip()) < 100:
        return jsonify({"error": "原稿が短すぎます（100文字以上必要）"}), 400

    # オプション
    try:
        target_count = int(request.form.get("target_count", "50"))
    except ValueError:
        target_count = 50
    target_count = max(5, min(target_count, 200))

    try:
        concurrency = int(request.form.get("concurrency", "12"))
    except ValueError:
        concurrency = 12
    concurrency = max(1, min(concurrency, 24))

    user_instructions = request.form.get("user_instructions", "").strip()
    worldview_preset = request.form.get("worldview_preset", "").strip()
    no_text_mode = request.form.get("no_text_mode") == "on"

    # ジョブ作成
    job_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "manuscript.txt").write_text(manuscript_text, encoding="utf-8")
    if user_instructions:
        (job_dir / "user_instructions.txt").write_text(user_instructions, encoding="utf-8")

    _set_job_state(
        job_id,
        status="queued",
        phase=0,
        message="キューに追加しました",
        percent=0,
        target_count=target_count,
        concurrency=concurrency,
        provider=provider,
        openai_quality=openai_quality if provider == PROVIDER_GPT_IMAGE else None,
    )

    thread = threading.Thread(
        target=_run_pipeline_thread,
        args=(job_id, manuscript_text, target_count, user_instructions, concurrency, provider, openai_quality, worldview_preset, no_text_mode),
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": job_id, "redirect": f"/progress/{job_id}"})


@app.route("/progress/<job_id>")
@login_required
def progress_page(job_id):
    return render_template("progress.html", job_id=job_id)


@app.route("/api/status/<job_id>")
@login_required
def api_status(job_id):
    state = _get_job_state(job_id)
    if not state:
        return jsonify({"status": "not_found"}), 404
    return jsonify(state)


@app.route("/api/items/<job_id>")
@login_required
def api_items(job_id):
    """画像の生成状況スナップショット"""
    snapshot = _image_snapshot(OUTPUT_DIR / job_id)
    return jsonify(snapshot)


@app.route("/api/logs/<job_id>")
@login_required
def api_logs(job_id):
    since = int(request.args.get("since", 0))
    with _jobs_lock:
        logs = list(_job_logs.get(job_id, []))
    if not logs:
        logs = load_json(OUTPUT_DIR / job_id / "logs.json", [])
    return jsonify({"logs": logs[since:], "total": len(logs)})


@app.route("/api/manifest/<job_id>")
@login_required
def api_manifest(job_id):
    manifest = load_json(OUTPUT_DIR / job_id / "manifest.json", {})
    return jsonify(manifest)


@app.route("/results/<job_id>/<path:filename>")
@login_required
def serve_results(job_id, filename):
    """画像など結果ファイルを配信"""
    result_dir = OUTPUT_DIR / job_id
    if not result_dir.exists():
        return "結果が見つかりません", 404
    return send_from_directory(str(result_dir), filename)


@app.route("/download/<job_id>")
@login_required
def download_zip(job_id):
    result_dir = OUTPUT_DIR / job_id
    if not result_dir.exists():
        return "結果が見つかりません", 404

    images = _completed_images(result_dir)
    if not images:
        return "ダウンロードできる画像はまだありません。生成が終わるまでお待ちください。", 409

    manifest = load_json(result_dir / "manifest.json", {})
    if not manifest:
        snapshot = _image_snapshot(result_dir, images)
        manifest = {
            "title": load_json(result_dir / "analysis.json", {}).get("title", job_id),
            "partial": True,
            "succeeded": len(images),
            "items": snapshot["items"],
        }
    title = manifest.get("title", job_id)
    safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:50] or job_id

    # 画像は圧縮済みなので再圧縮せず、一時ファイルから配信してメモリ消費を抑える。
    archive = tempfile.TemporaryFile(mode="w+b")
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
            for img in images:
                zf.write(img, f"images/{img.name}")
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2),
                        compress_type=zipfile.ZIP_DEFLATED)
            ms_path = result_dir / "manuscript.txt"
            if ms_path.exists():
                zf.write(ms_path, "manuscript.txt", compress_type=zipfile.ZIP_DEFLATED)

        size = archive.tell()
        archive.seek(0)
        response = send_file(archive, mimetype="application/zip", as_attachment=True,
                             download_name=f"{safe_title}_{job_id}.zip")
        response.content_length = size
        response.headers["Cache-Control"] = "no-store"
        response.call_on_close(archive.close)
        return response
    except Exception:
        archive.close()
        raise


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3001))
    print("\n" + "=" * 50)
    print("  図解つくーる 起動中...")
    print(f"  http://localhost:{port}")
    print("=" * 50 + "\n")
    app.run(host="0.0.0.0", port=port, debug=False)
