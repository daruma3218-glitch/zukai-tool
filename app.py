#!/usr/bin/env python3
"""図解つくーる - Flask Web アプリケーション

シンプルな1機能アプリ:
  原稿アップロード → N 枚の図解画像を並列生成 → ZIP ダウンロード
"""

import functools
import io
import json
import os
import secrets
import threading
import zipfile
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)

from utils import load_env, load_json
from pipeline import DiagramPipeline


PROJECT_ROOT = Path(__file__).parent
OUTPUT_DIR = PROJECT_ROOT / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# .env をロード
load_env(PROJECT_ROOT)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

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


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect(url_for("index"))
    if session.get("authenticated"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if request.form.get("password", "") == APP_PASSWORD:
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "パスワードが正しくありません"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ====== ジョブ管理 ======
def _set_job_state(job_id: str, **kwargs):
    with _jobs_lock:
        state = _jobs.setdefault(job_id, {})
        state.update(kwargs)
        state["updated_at"] = datetime.now().isoformat()
        # ファイルにも保存
        try:
            (OUTPUT_DIR / job_id / "job.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


def _get_job_state(job_id: str) -> dict:
    with _jobs_lock:
        if job_id in _jobs:
            return dict(_jobs[job_id])
    # ファイルから復元
    job_path = OUTPUT_DIR / job_id / "job.json"
    if job_path.exists():
        try:
            return json.loads(job_path.read_text(encoding="utf-8"))
        except Exception:
            pass
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
        (OUTPUT_DIR / job_id / "logs.json").write_text(
            json.dumps(_job_logs[job_id], ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def _run_pipeline_thread(job_id: str, manuscript_text: str, target_count: int,
                         user_instructions: str, concurrency: int):
    job_dir = OUTPUT_DIR / job_id
    try:
        _set_job_state(job_id, status="running", phase=0, message="開始しています...", percent=0)
        _add_log(job_id, "system", f"ジョブ {job_id} を開始（目標 {target_count} 枚 / 並列 {concurrency}）")

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
            concurrency=concurrency,
            progress_callback=on_progress,
            log_callback=on_log,
            item_callback=on_item,
        )
        manifest = pipeline.run()
        _set_job_state(
            job_id,
            status="completed",
            phase=4,
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
    return render_template(
        "upload.html",
        past_jobs=past_jobs[:30],
        has_anthropic=bool(os.environ.get("ANTHROPIC_API_KEY")),
        has_gemini=bool(os.environ.get("GEMINI_API_KEY")),
    )


@app.route("/start", methods=["POST"])
@login_required
def start_job():
    # API キー確認
    missing = []
    if not os.environ.get("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY")
    if not os.environ.get("GEMINI_API_KEY"):
        missing.append("GEMINI_API_KEY")
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
    )

    thread = threading.Thread(
        target=_run_pipeline_thread,
        args=(job_id, manuscript_text, target_count, user_instructions, concurrency),
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
    snapshot = load_json(OUTPUT_DIR / job_id / "images_progress.json", {"items": []})
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

    manifest = load_json(result_dir / "manifest.json", {})
    title = manifest.get("title", job_id)
    safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:50] or job_id

    # ZIP メモリ作成
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # 画像のみを zip に入れる
        images_dir = result_dir / "images"
        if images_dir.exists():
            for img in sorted(images_dir.iterdir()):
                if img.is_file() and img.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    zf.write(img, f"images/{img.name}")
        # マニフェストも入れる
        manifest_path = result_dir / "manifest.json"
        if manifest_path.exists():
            zf.write(manifest_path, "manifest.json")
        # 原稿も入れる
        ms_path = result_dir / "manuscript.txt"
        if ms_path.exists():
            zf.write(ms_path, "manuscript.txt")

    zip_buffer.seek(0)
    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{safe_title}_{job_id}.zip",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3001))
    print("\n" + "=" * 50)
    print("  図解つくーる 起動中...")
    print(f"  http://localhost:{port}")
    print("=" * 50 + "\n")
    app.run(host="0.0.0.0", port=port, debug=False)
