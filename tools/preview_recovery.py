"""Synthetic UI states only, localhost:5096; no production data or AI calls."""
from preview_retry import service, root, save_json, datetime
import image_edit
from flask import jsonify, request


# Exercise real startup reconciliation on a synthetic interrupted pipeline.
save_json(root / "job.json", {"status": "running", "phase": 3, "percent": 65,
    "title": "再起動後の中断・手直し待機（架空データ）", "started_at": datetime.now().isoformat()})
service.recover_interrupted_jobs()
# Static edit-state fixtures. Concurrency transitions use real threads in tests.
save_json(root / "edits.json", {"version": 1, "edits": [
    {"id": "q", "source": "diagram_001.png", "output": "diagram_001__e1.png", "label": "文字を全部消す",
     "status": "queued", "owner": image_edit.PROCESS_OWNER},
    {"id": "i", "source": "diagram_001.png", "output": "diagram_001__e2.png", "label": "指示して直す",
     "status": "interrupted", "error": "サーバーの再起動で中断しました。必要ならもう一度お試しください。"}
]})


@service.app.before_request
def disallow_preview_edits():
    if request.path.startswith("/api/edit/"):
        return jsonify({"error": "この画面は架空データの表示確認用です。"}), 400


if __name__ == "__main__":
    print("http://127.0.0.1:5096/progress/20261004_120000_preview", flush=True)
    service.app.run(host="127.0.0.1", port=5096, debug=False, use_reloader=False, threaded=True)
