"""生成完了・途中停止・再読込でも、保存済み画像を取り出せることを検証する。"""
import io
import json
from pathlib import Path
import sys
import zipfile
from unittest import mock

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as appmod
import generator
import pipeline
import subscription_runtime
import utils


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "test-password")
    monkeypatch.setattr(appmod, "_jobs", {})
    monkeypatch.setattr(appmod, "_job_logs", {})
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


def image_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (160, 90), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def saved_job(tmp_path, status="running"):
    root = tmp_path / "old-job"
    (root / "images").mkdir(parents=True)
    (root / "images/diagram_001.png").write_bytes(image_bytes())
    utils.save_json(root / "job.json", {"status": status, "phase": 4})
    (root / "manuscript.txt").write_text("原稿の保存テスト", encoding="utf-8")
    return root


def test_start_finishes_after_generation_without_any_review_call(client, tmp_path, monkeypatch):
    rows = [{"index": i, "excerpt": "原稿", "section": "第1章", "prompt": "diagram"}
            for i in (1, 2)]
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    monkeypatch.setattr(pipeline, "get_anthropic_client", lambda: object())
    monkeypatch.setattr(pipeline, "analyze_manuscript", lambda *a, **k: {"title": "完了テスト"})
    monkeypatch.setattr(pipeline, "extract_visual_points", lambda *a, **k: rows)
    monkeypatch.setattr(pipeline, "generate_all_prompts", lambda *a, **k: rows)

    def generate(**kwargs):
        generator._save_as_16_9(image_bytes(), kwargs["output_dir"] / "diagram_001.png")
        result = [dict(rows[0], success=True, filename="diagram_001.png"),
                  dict(rows[1], success=False, filename=None, error="fixture failure")]
        for item in result:
            kwargs["progress_callback"](dict(item, status="ok" if item["success"] else "failed"))
        return result

    class InlineThread:
        def __init__(self, target, args, **kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(appmod.threading, "Thread", InlineThread)
    with mock.patch.object(pipeline, "run_parallel_generation", side_effect=generate) as generation, \
         mock.patch.object(subscription_runtime, "generate", side_effect=AssertionError("unexpected review")) as review:
        response = client.post("/start", data={"manuscript_text": "テスト原稿。" * 25,
                                               "provider": "gpt-image", "openai_quality": "high",
                                               "target_count": "5", "concurrency": "2"})
    assert response.status_code == 200
    job_id = response.json["job_id"]
    state = client.get(f"/api/status/{job_id}").json
    assert (state["status"], state["phase"], state["percent"]) == ("completed", 3, 100)
    assert (state["succeeded"], state["failed"]) == (1, 1)
    review.assert_not_called()
    assert generation.call_args.kwargs["openai_quality"] == "high"
    assert generation.call_args.kwargs["concurrency"] == 2
    manifest = client.get(f"/api/manifest/{job_id}").json
    assert "content_review" not in manifest
    assert not (tmp_path / job_id / "content_review.json").exists()
    assert client.get(f"/api/items/{job_id}").json["available_images"] == 1
    response = client.get(f"/download/{job_id}")
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert archive.testzip() is None
        assert archive.read("images/diagram_001.png") == image_bytes()
        assert json.loads(archive.read("manifest.json"))["succeeded"] == 1
    response.close()


@pytest.mark.parametrize("status", ["running", "error", "completed"])
def test_saved_images_download_without_manifest_or_review(client, tmp_path, status):
    root = saved_job(tmp_path, status)
    (root / "images/.diagram_002.png.tmp").write_bytes(b"incomplete")
    (root / "images/diagram_003.png").touch()
    (root / "images/notes.txt").write_text("not an image")
    utils.save_json(root / "prompts.json", {"items": [{"index": 1, "section": "対応する章"}]})
    snapshot = client.get("/api/items/old-job")
    assert snapshot.headers["Cache-Control"] == "no-store"
    assert snapshot.json["available_images"] == 1
    assert snapshot.json["items"][0]["status"] == "ok"
    assert snapshot.json["items"][0]["section"] == "対応する章"
    created = []
    original = appmod.tempfile.TemporaryFile

    def tracked_file(**kwargs):
        file = original(**kwargs)
        created.append(file)
        return file

    with mock.patch.object(appmod.tempfile, "TemporaryFile", side_effect=tracked_file):
        response = client.get("/download/old-job")
    assert response.status_code == 200
    assert int(response.headers["Content-Length"]) == len(response.data)
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == ["images/diagram_001.png", "manifest.json", "manuscript.txt"]
        assert archive.getinfo("images/diagram_001.png").compress_type == zipfile.ZIP_STORED
        assert json.loads(archive.read("manifest.json"))["partial"] is True
    response.close()
    assert created[0].closed
    assert client.get("/results/old-job/images/diagram_001.png").data == image_bytes()


def test_manifest_restores_grid_after_missing_progress(client, tmp_path):
    root = saved_job(tmp_path)
    utils.save_json(root / "manifest.json", {"title": "旧ジョブ", "succeeded": 1,
                    "items": [{"index": 1, "success": True, "filename": "diagram_001.png",
                               "content_review": {"status": "unverified"}}]})
    snapshot = client.get("/api/items/old-job").json
    assert len(snapshot["items"]) == 1
    assert snapshot["items"][0]["status"] == "ok"
    assert snapshot["available_images"] == 1


def test_missing_file_is_not_reported_as_downloadable(client, tmp_path):
    root = tmp_path / "missing"
    utils.save_json(root / "images_progress.json", {"items": [
        {"index": 1, "status": "ok", "filename": "diagram_001.png"}]})
    snapshot = client.get("/api/items/missing").json
    assert snapshot["items"][0]["status"] == "failed"
    assert snapshot["available_images"] == 0
    assert client.get("/download/missing").status_code == 409
    assert client.get("/download/unknown").status_code == 404


def test_download_requires_login(client, tmp_path):
    saved_job(tmp_path)
    unauthenticated = appmod.app.test_client()
    assert unauthenticated.get("/download/old-job").status_code == 302
    assert unauthenticated.get("/api/items/old-job").status_code == 302


def test_latest_saved_state_wins_over_worker_cache(client, tmp_path):
    appmod._jobs["job"] = {"status": "running"}
    utils.save_json(tmp_path / "job/job.json", {"status": "completed", "phase": 3})
    assert client.get("/api/status/job").json["status"] == "completed"


def test_version_reports_removed_review(client):
    version = client.get("/version")
    assert version.json["image_review_enabled"] is False
    assert version.json["pipeline_phases"] == 3
    assert version.json["partial_download_enabled"] is True
    assert "image_review" not in version.json["editorial_models"]


def test_failed_json_replace_preserves_previous_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    utils.save_json(path, {"before": True})
    def fail(*args):
        raise OSError("fixture replace failure")
    monkeypatch.setattr(utils.os, "replace", fail)
    with pytest.raises(OSError):
        utils.save_json(path, {"after": True})
    assert json.loads(path.read_text()) == {"before": True}
    assert list(tmp_path.iterdir()) == [path]


def test_incomplete_image_is_never_published(tmp_path, monkeypatch):
    path = tmp_path / "diagram_001.png"
    def fail_save(self, filename, **kwargs):
        Path(filename).write_bytes(b"partial PNG")
        assert not path.exists()
        raise OSError("fixture image write failure")
    monkeypatch.setattr(Image.Image, "save", fail_save)
    with pytest.raises(OSError):
        generator._save_as_16_9(image_bytes_before_patch, path)
    assert list(tmp_path.iterdir()) == []


image_bytes_before_patch = image_bytes()
