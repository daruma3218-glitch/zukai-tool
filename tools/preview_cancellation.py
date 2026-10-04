"""Offline UI rehearsal. Binds localhost; uses only synthetic text and images.

Run from the repository: python tools/preview_cancellation.py
No image provider, worker queue or production data is contacted.
"""
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="zukai-cancel-preview-")
os.environ["OPENAI_API_KEY"] = "offline-preview-unused"
os.environ["ZUKAI_RETENTION_DAYS"] = "0"
import app as service
from PIL import Image, ImageDraw

service.APP_PASSWORD = ""
service.app.secret_key = "local-offline-preview"
service.retention.start_scheduler = lambda *args: None
service.retention.run_in_background = lambda *args: None


class PreviewPipeline:
    def __init__(self, **kwargs):
        self.args = kwargs

    def run(self):
        args = self.args
        args["progress_callback"](3, "画面テスト用：中止ボタンを押してください（AI生成なし）", 50)
        root = args["output_dir"]
        (root / "images").mkdir(exist_ok=True)
        image = Image.new("RGB", (960, 540), "#e8e0f5")
        ImageDraw.Draw(image).text((390, 255), "OFFLINE UI TEST", fill="#563187")
        image.save(root / "images/diagram_001.png")
        service.save_json(root / "images_progress.json", {"items": [
            {"index": 1, "status": "ok", "filename": "diagram_001.png", "keypoint": "保存済みのテスト画像"},
            {"index": 2, "status": "generating", "keypoint": "送信済みの処理を再現"},
            {"index": 3, "status": "pending", "keypoint": "待機中：中止後は生成しない"}]})
        until = time.monotonic() + 600
        while time.monotonic() < until:
            if service.Cancellation(root).requested():
                time.sleep(3)  # Represent a response that was already in flight.
                args["cancel_check"]()
            time.sleep(0.1)
        raise RuntimeError("オフライン画面テストの時間が終了しました")


service.DiagramPipeline = PreviewPipeline
if __name__ == "__main__":
    print("Offline preview: http://127.0.0.1:5094", flush=True)
    service.app.run(host="127.0.0.1", port=5094, debug=False, use_reloader=False, threaded=True)
