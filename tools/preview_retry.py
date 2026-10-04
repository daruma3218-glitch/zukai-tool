"""Synthetic-only browser rehearsal: localhost:5095, no external generation."""
from datetime import datetime, timedelta
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="zukai-retry-preview-")
os.environ["OPENAI_API_KEY"] = "offline-preview-unused"
os.environ["ZUKAI_RETENTION_DAYS"] = "0"
import app as service
import generator
from PIL import Image, ImageDraw
from utils import save_json

service.APP_PASSWORD = ""
service.app.secret_key = "local-offline-preview"
service.retention.start_scheduler = lambda *a: None
service.retention.run_in_background = lambda *a: None
service.job_health.runtime._api_enabled_for = lambda *a: False
service.job_health.runtime.cli_path = lambda *a: None
service.job_health.runtime._gateway_conf = lambda: True
service.job_health.worker_probe.sample = lambda: "unconfirmed"


class DisabledPipeline:
    def __init__(self, **kwargs):
        raise RuntimeError("新規生成はこの画面テストでは利用できません")


service.DiagramPipeline = DisabledPipeline


def picture(output, text, color):
    image = Image.new("RGB", (960, 540), color)
    ImageDraw.Draw(image).text((340, 255), text, fill="#443366")
    image.save(output)


def generate(self, prompt, output):
    print(f"OFFLINE_GENERATE {output.name}", flush=True)
    time.sleep(3)
    picture(output, "REGENERATED - OFFLINE TEST", "#e0f5e6")
    return True, ""


generator.ParallelImageGenerator._dispatch_sync_generate = generate

root = service.OUTPUT_DIR / "20261004_120000_preview"
(root / "images").mkdir(parents=True)
rows = [{"index": i, "prompt": f"Synthetic test {i}", "keypoint": title}
        for i, title in enumerate(["保存済み：この画像は保持", "失敗分：ここだけ再生成", "中止分：ここだけ再生成"], 1)]
picture(root / "images/diagram_001.png", "PRESERVED - OFFLINE TEST", "#e8e0f5")
save_json(root / "job.json", {"status": "cancelled", "title": "不足分の再生成・画面テスト", "phase": 3,
    "percent": 65, "started_at": datetime.now().isoformat(), "updated_at": datetime.now().isoformat()})
save_json(root / "prompts.json", {"items": rows})
save_json(root / "request.json", {"provider": "gpt-image", "openai_model": "gpt-image-2",
    "openai_quality": "high", "concurrency": 1})
save_json(root / "images_progress.json", {"items": [dict(r, status=s, filename=f"diagram_{r['index']:03}.png" if s == "ok" else None)
    for r, s in zip(rows, ["ok", "failed", "cancelled"])]})
save_json(root / "cancel_requested.json", {"requested_at": datetime.now().isoformat()})
(root / "manuscript.txt").write_text("架空のテスト原稿。", encoding="utf-8")

waiting = service.OUTPUT_DIR / "20261004_120001_wait"
waiting.mkdir()
save_json(waiting / "job.json", {"status": "running", "title": "長時間待機・画面テスト", "phase": 2,
    "percent": 40, "message": "候補の設計を待っています（架空の待機状態）", "started_at": (datetime.now() - timedelta(minutes=6)).isoformat(),
    "updated_at": (datetime.now() - timedelta(minutes=5)).isoformat()})
service._active_jobs.add(waiting.name)

if __name__ == "__main__":
    print("DATA", service.OUTPUT_DIR, flush=True)
    print("http://127.0.0.1:5095/progress/20261004_120000_preview", flush=True)
    print("http://127.0.0.1:5095/progress/20261004_120001_wait", flush=True)
    service.app.run(host="127.0.0.1", port=5095, debug=False, use_reloader=False, threaded=True)
