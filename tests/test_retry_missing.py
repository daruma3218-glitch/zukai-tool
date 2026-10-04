"""Resume integration using the actual generator scheduler, with no external AI."""
import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app as service
import generator
import retention
import retry_missing
from job_control import Cancellation
from test_job_cancellation import client, png
from utils import load_json, save_json


@pytest.fixture
def saved_job(client, tmp_path):
    path = tmp_path / "20261004_120000_abcdef"
    path.mkdir()
    save_json(path / "job.json", {"status": "cancelled", "phase": 3, "percent": 60,
        "started_at": "2026-10-04T12:00:00", "updated_at": "2026-10-04T12:02:00"})
    save_json(path / "request.json", {"provider": "gpt-image", "openai_model": "gpt-image-2",
        "openai_quality": "high", "concurrency": 1})
    rows = [{"index": i, "group": 1, "variant": letter, "filename": f"diagram_001_{letter}.png",
             "prompt": f"saved prompt {i}", "keypoint": f"候補{letter}"}
            for i, letter in enumerate("abc", 1)]
    save_json(path / "prompts.json", {"items": rows})
    save_json(path / "images_progress.json", {"items": [dict(row, status=status) for row, status in
        zip(rows, ["failed", "failed", "cancelled"])]})  # Disk, not stale state, determines what is kept.
    save_json(path / "manifest.json", {"title": "saved title", "custom": "keep"})
    save_json(path / "cancel_requested.json", {"requested_at": "original stop"})
    save_json(path / "adoption.json", {"adopted": {"diagram_001_a.png": {"group": 1}}})
    save_json(path / "edits.json", {"edits": [{"source": "diagram_001_a.png", "status": "ok"}]})
    (path / "manuscript.txt").write_text("保存済みの原稿", encoding="utf-8")
    png(path / "images/diagram_001_a.png")
    return path


def preview(client, path):
    return client.get(f"/api/retry-missing/{path.name}")


def submit(client, path, token):
    return client.post(f"/api/retry-missing/{path.name}", json={"plan_token": token},
                       headers={"X-CSRF-Token": "fixture-csrf"})


def capture_thread(monkeypatch):
    threads = []
    class Thread:
        def __init__(self, target, args, daemon):
            self.target, self.args = target, args
            threads.append(self)
        def start(self):
            pass
        def finish(self):
            self.target(*self.args)
    monkeypatch.setattr(service, "threading", SimpleNamespace(Thread=Thread))
    return threads


def test_plan_keeps_real_images_and_requires_fresh_confirmation(client, saved_job, monkeypatch):
    threads = capture_thread(monkeypatch)
    p = preview(client, saved_job).json
    assert (p["count"], p["kept"], p["ai_images"], p["maps"]) == (2, 1, 2, 0)
    assert (p["model"], p["quality"], p["concurrency"]) == ("gpt-image-2", "high", 1)
    assert submit(client, saved_job, "0" * 64).status_code == 409
    assert submit(client, saved_job, p["plan_token"]).status_code == 202
    assert submit(client, saved_job, p["plan_token"]).status_code == 409
    assert len(threads) == 1
    assert not Cancellation(saved_job).requested()
    assert not retention.is_finished(saved_job)
    assert next((saved_job / "retry_history").glob("*/cancel_requested.json")).is_file()


def test_retry_only_missing_with_frozen_settings_and_preserves_assets(client, saved_job, monkeypatch):
    protected = [saved_job / name for name in ("prompts.json", "manuscript.txt", "adoption.json", "edits.json", "images/diagram_001_a.png")]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    threads = capture_thread(monkeypatch)
    calls = []
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    monkeypatch.setenv("OPENAI_IMAGE_MODEL", "gpt-image-2.5-sunburst")
    def generate(self, prompt, output):
        calls.append((self.openai_model, self.openai_quality, prompt, output.name))
        png(output)
        return True, ""
    monkeypatch.setattr(generator.ParallelImageGenerator, "_dispatch_sync_generate", generate)
    p = preview(client, saved_job).json
    assert submit(client, saved_job, p["plan_token"]).status_code == 202
    threads[0].finish()
    assert [c[3] for c in calls] == ["diagram_001_b.png", "diagram_001_c.png"]
    assert all(c[:2] == ("gpt-image-2", "high") for c in calls)
    assert "saved prompt 2" in calls[0][2] and "saved prompt 3" in calls[1][2]
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in protected} == before
    manifest = load_json(saved_job / "manifest.json")
    assert (manifest["succeeded"], manifest["failed"], manifest["custom"]) == (3, 0, "keep")
    assert all(i["success"] for i in manifest["items"])
    assert saved_job.name not in service._active_jobs
    assert preview(client, saved_job).json["code"] == "nothing_missing"


def test_stop_during_retry_saves_inflight_and_leaves_the_rest(client, saved_job, monkeypatch):
    threads = capture_thread(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    calls = []
    def generate(self, prompt, output):
        calls.append(output.name)
        response = client.post(f"/api/cancel/{saved_job.name}", json={}, headers={"X-CSRF-Token": "fixture-csrf"})
        assert response.status_code == 202
        png(output)
        return True, ""
    monkeypatch.setattr(generator.ParallelImageGenerator, "_dispatch_sync_generate", generate)
    old = preview(client, saved_job).json["plan_token"]
    submit(client, saved_job, old)
    threads[0].finish()
    assert calls == ["diagram_001_b.png"]
    assert service._get_job_state(saved_job.name)["status"] == "cancelled"
    assert [i["status"] for i in load_json(saved_job / "images_progress.json")["items"]] == ["ok", "ok", "cancelled"]
    new = preview(client, saved_job).json
    assert (new["count"], new["kept"]) == (1, 2)
    assert new["plan_token"] != old
    assert submit(client, saved_job, old).status_code == 409


def test_cancel_before_retry_thread_starts_never_generates(client, saved_job, monkeypatch):
    threads = capture_thread(monkeypatch)
    generate = Mock(side_effect=AssertionError("no generation"))
    monkeypatch.setattr(retry_missing, "generate", generate)
    submit(client, saved_job, preview(client, saved_job).json["plan_token"])
    client.post(f"/api/cancel/{saved_job.name}", json={}, headers={"X-CSRF-Token": "fixture-csrf"})
    threads[0].finish()
    generate.assert_not_called()
    assert service._get_job_state(saved_job.name)["status"] == "cancelled"


def test_login_csrf_and_cleanup_lock(client, saved_job):
    url = f"/api/retry-missing/{saved_job.name}"
    token = preview(client, saved_job).json["plan_token"]
    assert service.app.test_client().post(url, json={}).status_code == 302
    assert client.post(url, json={"plan_token": token}).status_code == 403
    assert client.post(url, data="{}", headers={"X-CSRF-Token": "fixture-csrf"}).status_code == 403
    lock = retention._acquire_lock(saved_job.parent)
    try:
        assert submit(client, saved_job, token).status_code == 409
        assert lock.exists()
        assert Cancellation(saved_job).requested()
    finally:
        lock.unlink()
    assert submit(client, saved_job, "invalid").status_code == 409
    assert not (saved_job.parent / retention.LOCK_NAME).exists()


@pytest.mark.parametrize("case,code", [("trimmed", "retention"), ("active", "active"),
    ("no_plan", "no_plan"), ("invalid_file", "invalid_plan"), ("duplicate", "invalid_plan"),
    ("missing_prompt", "incomplete_plan"), ("unknown_model", "missing_settings"),
    ("old_gemini", "missing_settings"), ("malformed", "invalid_plan")])
def test_ineligible_jobs_do_not_start(client, saved_job, case, code):
    rows = load_json(saved_job / "prompts.json")
    settings = load_json(saved_job / "request.json")
    if case == "trimmed":
        save_json(saved_job / retention.TRIM_MARKER, {})
    elif case == "active":
        service._active_jobs.add(saved_job.name)
    elif case == "no_plan":
        (saved_job / "prompts.json").unlink()
    elif case == "invalid_file":
        rows["items"][1]["filename"] = "../escape.png"
        save_json(saved_job / "prompts.json", rows)
    elif case == "duplicate":
        rows["items"][1]["index"] = 1
        save_json(saved_job / "prompts.json", rows)
    elif case == "missing_prompt":
        rows["items"][1]["prompt"] = ""
        save_json(saved_job / "prompts.json", rows)
    elif case == "unknown_model":
        settings["openai_model"] = "unrecognized-model"
        save_json(saved_job / "request.json", settings)
    elif case == "old_gemini":
        settings["provider"] = "nanobanana"
        save_json(saved_job / "request.json", settings)
    else:
        save_json(saved_job / "prompts.json", [])
    before = (saved_job / "job.json").read_bytes()
    result = preview(client, saved_job)
    assert result.status_code == 200 and result.json["code"] == code
    assert submit(client, saved_job, "0" * 64).status_code == 409
    assert (saved_job / "job.json").read_bytes() == before


def test_failure_can_retry_again_but_old_confirmation_cannot_replay(client, saved_job, monkeypatch):
    threads = capture_thread(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    monkeypatch.setattr(generator.ParallelImageGenerator, "_dispatch_sync_generate", lambda *a: (False, "fixture error"))
    old = preview(client, saved_job).json["plan_token"]
    submit(client, saved_job, old)
    threads[0].finish()
    p = preview(client, saved_job).json
    assert p["count"] == 2 and p["plan_token"] != old
    assert service._get_job_state(saved_job.name)["failed"] == 2
    assert submit(client, saved_job, old).status_code == 409


def test_thread_start_failure_releases_ownership_and_cleanup_lock(client, saved_job, monkeypatch):
    thread = Mock()
    thread.start.side_effect = RuntimeError("cannot start")
    monkeypatch.setattr(service, "threading", SimpleNamespace(Thread=lambda **kw: thread))
    response = submit(client, saved_job, preview(client, saved_job).json["plan_token"])
    assert response.status_code == 503 and response.json["error"]
    assert service._get_job_state(saved_job.name)["status"] == "error"
    assert saved_job.name not in service._active_jobs
    assert not (saved_job.parent / retention.LOCK_NAME).exists()
    assert preview(client, saved_job).json["enabled"]


def test_map_only_needs_no_image_ai_key(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import map_renderer
    def render(spec, output, **kwargs):
        png(output)
        return True, "", []
    monkeypatch.setattr(map_renderer, "render_map_file", render)
    results = generator.run_parallel_generation([{"index": 1, "render": "map", "map_spec": {"countries": ["Japan"]}}],
        tmp_path / "images", skip_existing=True)
    assert results[0]["success"]


def test_saved_file_appearing_before_dispatch_is_never_overwritten(tmp_path, monkeypatch):
    output = tmp_path / "images/diagram_001.png"
    png(output)
    before = output.read_bytes()
    dispatch = Mock(side_effect=AssertionError("completed image must not regenerate"))
    monkeypatch.setattr(generator.ParallelImageGenerator, "_dispatch_sync_generate", dispatch)
    results = generator.run_parallel_generation([{"index": 1, "prompt": "test"}], tmp_path / "images",
        provider="gpt-image", openai_api_key="fixture", skip_existing=True)
    assert results[0]["skipped"]
    assert output.read_bytes() == before
    dispatch.assert_not_called()
