"""Real control flow with mocked AI: no external requests or paid generation."""
import io
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as service
import generator
import pipeline
import prompter
import retention
import subscription_runtime as runtime
from job_control import Cancellation, JobCancelled
from utils import load_json, save_json


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(service, "APP_PASSWORD", "fixture")
    monkeypatch.setattr(service, "_jobs", {})
    monkeypatch.setattr(service, "_job_logs", {})
    monkeypatch.setattr(service, "_active_jobs", set())
    monkeypatch.setattr(retention, "start_scheduler", lambda *a: None)
    monkeypatch.setattr(retention, "run_in_background", lambda *a: None)
    c = service.app.test_client()
    with c.session_transaction() as session:
        session.update(authenticated=True, cancel_csrf="fixture-csrf")
    return c


def job(root, name="20261004_100000_abcdef", status="running", active=True):
    path = root / name
    path.mkdir()
    save_json(path / "job.json", {"status": status, "phase": 1, "percent": 12,
                                 "target_count": 5, "started_at": "2026-10-04T10:00:00"})
    (path / "manuscript.txt").write_text("原稿テスト。" * 30, encoding="utf-8")
    if active:
        service._active_jobs.add(name)
    return path


def cancel(c, path):
    return c.post(f"/api/cancel/{path.name}", json={}, headers={"X-CSRF-Token": "fixture-csrf"})


def png(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (160, 90), "white").save(path)


def test_stop_requires_login_json_and_csrf(client, tmp_path):
    path = job(tmp_path)
    assert service.app.test_client().post(f"/api/cancel/{path.name}", json={}).status_code == 302
    assert client.get(f"/api/cancel/{path.name}").status_code == 405
    assert client.post(f"/api/cancel/{path.name}", json={}).status_code == 403
    assert not Cancellation(path).requested()
    assert cancel(client, tmp_path / "missing").status_code == 404
    assert cancel(client, tmp_path / "..").status_code == 404


def test_stop_is_durable_idempotent_and_cannot_be_overwritten(client, tmp_path):
    path = job(tmp_path)
    other = job(tmp_path, "20261004_100001_abcdef")
    assert cancel(client, path).status_code == 202
    marker = (path / "cancel_requested.json").read_bytes()
    service._jobs.clear()  # Read back from disk as after a cache loss.
    assert cancel(client, path).json["status"] == "cancelling"
    assert (path / "cancel_requested.json").read_bytes() == marker
    assert len(load_json(path / "logs.json")) == 1
    for status in ("running", "completed"):
        with pytest.raises(JobCancelled):
            service._set_job_state(path.name, status=status)
    assert service._get_job_state(path.name)["status"] == "cancelling"
    assert service._get_job_state(other.name)["status"] == "running"


@pytest.mark.parametrize("status", ["completed", "error", "cancelled"])
def test_terminal_job_is_not_reset(client, tmp_path, status):
    path = job(tmp_path, status=status)
    before = (path / "job.json").read_bytes()
    assert cancel(client, path).json["status"] == status
    assert (path / "job.json").read_bytes() == before
    assert not Cancellation(path).requested()


def test_orphan_stop_keeps_images_adoption_logs_and_zip(client, tmp_path):
    path = job(tmp_path, active=False)
    png(path / "images/diagram_001.png")
    save_json(path / "images_progress.json", {"items": [
        {"index": 1, "status": "ok", "filename": "diagram_001.png"},
        {"index": 2, "status": "generating"}, {"index": 3, "status": "pending"},
        {"index": 4, "status": "failed"}]})
    save_json(path / "logs.json", [{"message": "earlier event"}])
    save_json(path / "adoption.json", {"adopted": {"diagram_001.png": {"group": 1}}})
    adopted = (path / "adoption.json").read_bytes()
    response = cancel(client, path)
    assert response.json["status"] == "cancelled"
    assert (response.json["succeeded"], response.json["failed"], response.json["cancelled"]) == (1, 1, 2)
    assert (path / "adoption.json").read_bytes() == adopted
    assert load_json(path / "logs.json")[0]["message"] == "earlier event"
    items = client.get(f"/api/items/{path.name}").json
    assert [i["status"] for i in items["items"]] == ["ok", "cancelled", "cancelled", "failed"]
    downloaded = client.get(f"/download/{path.name}")
    assert downloaded.status_code == 200
    with zipfile.ZipFile(io.BytesIO(downloaded.data)) as archive:
        assert any(name.endswith("diagram_001.png") for name in archive.namelist())
    assert retention.is_finished(path)
    assert path in retention._job_dirs(tmp_path)


def test_cancel_before_thread_starts_never_calls_pipeline(client, tmp_path, monkeypatch):
    path = job(tmp_path, status="queued")
    cancel(client, path)
    factory = Mock(side_effect=AssertionError("must not start"))
    monkeypatch.setattr(service, "DiagramPipeline", factory)
    service._run_pipeline_thread(path.name, "原稿", 5, "", 2)
    factory.assert_not_called()
    assert service._get_job_state(path.name)["status"] == "cancelled"
    assert path.name not in service._active_jobs


def test_cancel_during_analysis_does_not_extract_or_generate(client, tmp_path, monkeypatch):
    path = job(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    monkeypatch.setattr(pipeline, "get_anthropic_client", lambda: object())
    def analyze(*args, **kwargs):
        cancel(client, path)
        return {"title": "unused late response"}
    monkeypatch.setattr(pipeline, "analyze_manuscript", analyze)
    extract = Mock(side_effect=AssertionError("must not extract"))
    monkeypatch.setattr(pipeline, "extract_visual_points", extract)
    service._run_pipeline_thread(path.name, "原稿", 5, "", 2, provider="gpt-image")
    assert service._get_job_state(path.name)["status"] == "cancelled"
    extract.assert_not_called()


def test_design_stop_skips_queued_batches_and_fallback(tmp_path, monkeypatch):
    token = Cancellation(tmp_path)
    called = []
    def query(*args, **kwargs):
        kwargs["cancel_check"]()
        called.append(1)
        save_json(token.path, {})
        kwargs["cancel_check"]()
    monkeypatch.setattr(prompter, "claude_query", query)
    rows = [{"index": i + 1, "excerpt": "原稿"} for i in range(25)]
    with pytest.raises(JobCancelled):
        prompter.generate_all_prompts(object(), rows, "test", max_workers=1, cancel_check=token.check)
    assert called == [1]


def test_parallel_stop_drains_sent_image_and_skips_the_rest(tmp_path, monkeypatch):
    token = Cancellation(tmp_path)
    calls, events = [], []
    def generate(self, prompt, output):
        calls.append(output.name)
        save_json(token.path, {})
        png(output)  # An already-sent image returns after stop.
        return True, ""
    monkeypatch.setattr(generator.ParallelImageGenerator, "_dispatch_sync_generate", generate)
    rows = [{"index": i + 1, "prompt": "test"} for i in range(6)]
    results = generator.run_parallel_generation(rows, tmp_path / "images", provider="gpt-image",
        openai_api_key="fixture", concurrency=1, progress_callback=events.append, cancel_check=token.check)
    assert calls == ["diagram_001.png"]
    assert results[0]["success"] is True
    assert all(r["status"] == "cancelled" for r in results[1:])
    assert sum(e["status"] == "generating" for e in events) == 1
    assert sum(e["status"] == "cancelled" for e in events) == 5


def test_two_inflight_images_finish_but_no_third_request_starts(tmp_path, monkeypatch):
    token = Cancellation(tmp_path)
    barrier, stopped = threading.Barrier(2), threading.Event()
    calls = []
    def generate(self, prompt, output):
        calls.append(output.name)
        leader = barrier.wait(timeout=3)
        if leader == 0:
            save_json(token.path, {})
            stopped.set()
        assert stopped.wait(3)
        png(output)
        return True, ""
    monkeypatch.setattr(generator.ParallelImageGenerator, "_dispatch_sync_generate", generate)
    rows = [{"index": i + 1, "prompt": "test"} for i in range(10)]
    results = generator.run_parallel_generation(rows, tmp_path / "images", provider="gpt-image",
        openai_api_key="fixture", concurrency=2, cancel_check=token.check)
    assert sorted(calls) == ["diagram_001.png", "diagram_002.png"]
    assert sum(r["success"] for r in results) == 2
    assert sum(r.get("status") == "cancelled" for r in results) == 8


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_stop_interrupts_rate_limit_retry(provider, tmp_path):
    token = Cancellation(tmp_path)
    def rate_limit(**kwargs):
        save_json(token.path, {})
        raise RuntimeError("429 rate limit")
    generate = Mock(side_effect=rate_limit)
    client = SimpleNamespace(images=SimpleNamespace(generate=generate), models=SimpleNamespace(generate_content=generate))
    fn = generator._sync_generate_image_openai if provider == "openai" else generator._sync_generate_image_gemini
    with pytest.raises(JobCancelled):
        fn(client, "prompt", tmp_path / "out.png", cancel_check=token.check)
    assert generate.call_count == 1


def test_gateway_stop_removes_only_its_request_without_failure_notice(tmp_path, monkeypatch):
    token, calls = Cancellation(tmp_path), []
    monkeypatch.setattr(runtime, "_worker_heartbeat", lambda: (True, ""))
    def storage(method, path, body=None):
        calls.append((method, path))
        if method == "POST":
            save_json(token.path, {})
            return 201, {}
        return 200, {}
    monkeypatch.setattr(runtime, "_storage", storage)
    terminal = Mock(side_effect=AssertionError("no failure notification"))
    monkeypatch.setattr(runtime, "_terminal", terminal)
    with pytest.raises(JobCancelled):
        runtime._gateway_generate({"id": "owned-request", "use_search": False, "timeout": 900}, cancel_check=token.check)
    assert calls == [("POST", "req/owned-request.json"), ("DELETE", "req/owned-request.json")]
    terminal.assert_not_called()


@pytest.mark.parametrize("mode", ["local", "api"])
def test_stop_prevents_llm_retry_and_provider_fallback(tmp_path, monkeypatch, mode):
    token, calls = Cancellation(tmp_path), []
    def invoke(*args):
        calls.append(args)
        save_json(token.path, {})
        raise RuntimeError("provider failed while user cancelled")
    monkeypatch.setattr(runtime, "_invoke", invoke)
    monkeypatch.setattr(runtime, "_api_claude", invoke)
    monkeypatch.setattr(runtime, "_api_key", lambda *a: "fixture")
    terminal = Mock(side_effect=AssertionError("no failure notice"))
    monkeypatch.setattr(runtime, "_terminal", terminal)
    request = {"primary": "claude", "model": "sonnet", "workload": "assets_plan"}
    with pytest.raises(JobCancelled):
        if mode == "local":
            runtime.run_local_request(request, cancel_check=token.check)
        else:
            runtime.run_api_request(request, conf={"tools": ["zukai"]}, cancel_check=token.check)
    assert len(calls) == 1
    terminal.assert_not_called()


def test_gateway_already_stopped_never_reads_or_submits(tmp_path, monkeypatch):
    token = Cancellation(tmp_path)
    save_json(token.path, {})
    storage = Mock(side_effect=AssertionError("no external I/O"))
    monkeypatch.setattr(runtime, "_storage", storage)
    with pytest.raises(JobCancelled):
        runtime._gateway_generate({"id": "stopped"}, cancel_check=token.check)
    storage.assert_not_called()


def test_restart_restores_original_places_not_candidate_image_count(client, tmp_path):
    path = job(tmp_path, status="cancelled")
    settings = {"target_count": 10, "concurrency": 3, "candidate_mode": "pair", "map_mode": "ai",
                "provider": "gpt-image", "openai_quality": "high", "openai_model": "gpt-image-2",
                "worldview_preset": "russia", "no_text_mode": True, "user_instructions": "設定を保持"}
    save_json(path / "request.json", settings)
    save_json(path / "manifest.json", {"images_planned": 20, "target_count": 20})
    response = client.get(f"/api/restart-settings/{path.name}")
    assert response.json["settings"] == settings
    assert response.json["manuscript_text"] == (path / "manuscript.txt").read_text(encoding="utf-8")
    assert service.app.test_client().get(f"/api/restart-settings/{path.name}").status_code == 302


def test_two_starts_never_share_a_job_directory(client, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    class ParkedThread:
        def __init__(self, **kwargs): pass
        def start(self): pass
    monkeypatch.setattr(service.threading, "Thread", ParkedThread)
    data = {"manuscript_text": "同じ秒の新規ジョブ。" * 20, "provider": "gpt-image", "target_count": "5"}
    first, second = client.post("/start", data=data), client.post("/start", data=data)
    assert first.status_code == second.status_code == 200
    assert first.json["job_id"] != second.json["job_id"]
    first_path = tmp_path / first.json["job_id"]
    cancel(client, first_path)
    assert not Cancellation(tmp_path / second.json["job_id"]).requested()
