"""Restart, long queue, cache and retention regressions; never call an AI."""
from pathlib import Path
import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app as service
import image_edit
import image_resources
import generator
import deploy_guard
import migration_entry
import resource_diagnostics as diagnostics
import retention
from test_job_cancellation import client, job, png
from utils import load_json, save_json


def test_restart_preserves_completed_images_and_exposes_missing_retry(client, tmp_path):
    path = job(tmp_path, active=False)
    png(path / "images" / "diagram_001.png")
    save_json(path / "prompts.json", {"items": [{"index": n, "filename": f"diagram_{n:03}.png", "prompt": "test"} for n in (1, 2)]})
    save_json(path / "images_progress.json", {"items": [
        {"index": 1, "filename": "diagram_001.png", "status": "running"},
        {"index": 2, "filename": "diagram_002.png", "status": "pending"}]})
    save_json(path / "request.json", {"provider": "gpt-image", "openai_model": generator.DEFAULT_OPENAI_MODEL,
                                      "openai_quality": "medium", "concurrency": 2})
    preserved = {p.name: p.read_bytes() for p in (path / "images" / "diagram_001.png", path / "manuscript.txt", path / "request.json", path / "prompts.json")}
    original = (path / "job.json").read_bytes()
    service.recover_interrupted_jobs()
    state = client.get(f"/api/status/{path.name}").json
    assert state["status"] == "interrupted" and state["succeeded"] == 1
    assert not state["wait"]["show"]
    items = client.get(f"/api/items/{path.name}").json["items"]
    assert [i["status"] for i in items] == ["ok", "interrupted"]
    quote = client.get(f"/api/retry-missing/{path.name}").json
    assert quote["enabled"] and quote["count"] == 1 and quote["kept"] == 1
    assert next(path.glob("recovery_history/*/job.json")).read_bytes() == original
    for name, contents in preserved.items():
        p = path / "images" / name if name.endswith(".png") else path / name
        assert p.read_bytes() == contents
    after = (path / "job.json").read_bytes()
    service.recover_interrupted_jobs()
    assert (path / "job.json").read_bytes() == after
    assert not (path / "cancel_requested.json").exists()


def test_recovery_never_interrupts_current_or_terminal_jobs(client, tmp_path):
    active = job(tmp_path)
    complete = job(tmp_path, name="completed", status="completed", active=False)
    before = [p.joinpath("job.json").read_bytes() for p in (active, complete)]
    service.recover_interrupted_jobs()
    assert [p.joinpath("job.json").read_bytes() for p in (active, complete)] == before


def test_restart_clears_only_old_metadata_locks(client, tmp_path):
    path = job(tmp_path, active=False)
    for lock in (path / ".edits.json.lock", path / ".adoption.json.lock", tmp_path / retention.LOCK_NAME):
        lock.touch()
        os.utime(lock, (image_edit.PROCESS_STARTED_AT - 1,) * 2)
    service.recover_interrupted_jobs()
    assert not list(path.glob(".*.lock"))
    assert not (tmp_path / retention.LOCK_NAME).exists()
    current = path / ".edits.json.lock"
    current.touch()
    image_edit.release_previous_worker_lock(current)
    assert current.exists()


@pytest.mark.parametrize("mode,marker,expected", [("active", False, 1), ("readonly", False, 0), ("active", True, 0)])
def test_factory_recovers_before_traffic_only_if_writable(tmp_path, monkeypatch, mode, marker, expected):
    module = SimpleNamespace(app=Mock(), recover_interrupted_jobs=Mock())
    for key, value in {"APP_PASSWORD": "fixture", "SECRET_KEY": "fixture", "DATA_DIR": str(tmp_path), "MIGRATION_ACCESS": mode}.items():
        monkeypatch.setenv(key, value)
    if marker:
        (tmp_path / ".migration-readonly").touch()
    monkeypatch.setattr(migration_entry.importlib, "import_module", lambda name: module)
    def install(mod, root):
        assert mod.recover_interrupted_jobs.call_count == expected
        return mod.app
    monkeypatch.setattr(deploy_guard, "install", install)
    gate = migration_entry.create_app()
    assert module.recover_interrupted_jobs.call_count == 0  # Parent process must do no recovery.
    gate({"REQUEST_METHOD": "GET", "PATH_INFO": "/version"}, Mock())
    gate({"REQUEST_METHOD": "GET", "PATH_INFO": "/version"}, Mock())
    assert module.recover_interrupted_jobs.call_count == expected


def test_worker_replacement_reinitializes_recovery_and_watcher(tmp_path, monkeypatch):
    module = SimpleNamespace(app=Mock(), recover_interrupted_jobs=Mock())
    install = Mock(return_value=module.app)
    monkeypatch.setattr(deploy_guard, "install", install)
    pid = [100]
    monkeypatch.setattr(migration_entry.os, "getpid", lambda: pid[0])
    runtime = migration_entry.WorkerRuntime(module, tmp_path, "active")
    runtime({}, Mock())
    runtime({}, Mock())
    assert module.recover_interrupted_jobs.call_count == 1 and install.call_count == 1
    pid[0] = 101
    runtime({}, Mock())
    assert module.recover_interrupted_jobs.call_count == 2 and install.call_count == 2


def test_forked_worker_gets_a_distinct_edit_owner(monkeypatch):
    old = image_edit.PROCESS_OWNER
    # Restore module globals after the simulated fork.
    monkeypatch.setattr(image_edit, "PROCESS_OWNER", old)
    monkeypatch.setattr(image_edit, "PROCESS_STARTED_AT", image_edit.PROCESS_STARTED_AT)
    monkeypatch.setattr(image_edit, "_OWNER_PID", image_edit._OWNER_PID)
    monkeypatch.setattr(image_edit.os, "getpid", lambda: 999999)
    image_edit.bind_worker()
    assert image_edit.PROCESS_OWNER != old
    first = image_edit.PROCESS_OWNER
    image_edit.bind_worker()
    assert image_edit.PROCESS_OWNER == first


def edit_job(tmp_path):
    path = tmp_path / "edit_job"
    png(path / "images" / "diagram_001.png")
    save_json(path / "job.json", {"status": "completed", "updated_at": "2020-01-01T00:00:00"})
    return path


def test_long_queue_stays_owned_and_deduplicated(tmp_path, monkeypatch):
    path = edit_job(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    threads = []
    monkeypatch.setattr(image_edit.threading, "Thread", lambda **kw: threads.append(kw) or Mock())
    first = image_edit.request_edit(path, "diagram_001.png", "instruct", "test")
    assert first["status"] == "queued"
    data = load_json(path / "edits.json")
    data["edits"][0]["created_at"] = "2000-01-01T00:00:00"
    save_json(path / "edits.json", data)
    again = image_edit.request_edit(path, "diagram_001.png", "instruct", "test")
    assert again["duplicate"] and again["id"] == first["id"] and len(threads) == 1
    assert image_edit.load_edits(path)[0]["status"] == "queued"
    assert image_edit.recover_interrupted_edits(path) == 0
    assert not retention.is_finished(path)


def test_edit_recovery_retains_saved_output_and_never_restarts(tmp_path, monkeypatch):
    path = edit_job(tmp_path)
    png(path / "images" / "diagram_001__e1.png")
    entries = [{"id": str(n), "source": "diagram_001.png", "output": f"diagram_001__e{n}.png",
                "status": "running", "owner": "old-worker"} for n in (1, 2)]
    save_json(path / "edits.json", {"version": 1, "edits": entries})
    original = (path / "edits.json").read_bytes()
    monkeypatch.setattr(image_edit.threading, "Thread", lambda **kw: pytest.fail("must not auto-restart"))
    assert image_edit.load_edits(path) == entries  # Reads do not alter status.
    assert image_edit.recover_interrupted_edits(path) == 2
    assert [e["status"] for e in image_edit.load_edits(path)] == ["ok", "interrupted"]
    assert next(path.glob("recovery_history/*/edits.json")).read_bytes() == original
    assert image_edit.recover_interrupted_edits(path) == 0


def test_edit_transitions_only_after_shared_slot_and_closes_client(tmp_path, monkeypatch):
    path = edit_job(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(image_resources, "_slots", slots)
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    sdk = Mock()
    # A no-image response keeps this test independent of image model details.
    def request(**kw):
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(data=[])
    sdk.images.edit.side_effect = request
    constructor = Mock(return_value=sdk)
    monkeypatch.setattr("openai.OpenAI", constructor)
    original = image_edit._write
    def write(p, data):
        original(p, data)
        if p.name == "edits.json" and data["edits"][0]["status"] == "failed":
            done.set()
    monkeypatch.setattr(image_edit, "_write", write)
    first = image_edit.request_edit(path, "diagram_001.png", "instruct", "test")
    try:
        assert first["status"] == "queued" and not entered.is_set()
        assert image_edit.load_edits(path)[0]["status"] == "queued"
        slots.release()
        assert entered.wait(3)
        state = image_edit.load_edits(path)[0]
        assert state["status"] == "running" and state["started_at"]
        assert image_edit.request_edit(path, "diagram_001.png", "instruct", "test")["duplicate"]
        constructor.assert_called_once_with(api_key="fixture", timeout=180, max_retries=0)
    finally:
        release.set()
    assert done.wait(3)
    sdk.close.assert_called_once()


def test_thread_start_failure_releases_edit_reservation(tmp_path, monkeypatch):
    path = edit_job(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    thread = Mock()
    thread.start.side_effect = RuntimeError("cannot start")
    monkeypatch.setattr(image_edit.threading, "Thread", lambda **kw: thread)
    result = image_edit.request_edit(path, "diagram_001.png", "instruct", "test")
    assert result["status"] == "failed"
    assert image_edit.load_edits(path)[0]["status"] == "failed"


def test_retention_cannot_take_source_while_edit_is_reserved(tmp_path, monkeypatch):
    path = edit_job(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    lock = retention._acquire_lock(tmp_path)
    try:
        with pytest.raises(image_edit.EditError, match="整理中"):
            image_edit.request_edit(path, "diagram_001.png", "remove_text")
        assert not (path / "edits.json").exists()
    finally:
        lock.unlink()


def test_terminal_cache_eviction_keeps_status_and_logs_on_disk(client, tmp_path):
    path = job(tmp_path)
    service._set_job_state(path.name, status="running", phase=3)
    service._add_log(path.name, "system", "saved log")
    assert path.name in service._jobs and path.name in service._job_logs
    service._set_job_state(path.name, status="completed")
    service._add_log(path.name, "system", "completion")
    service._release_job_resources(path.name, "generation")
    assert path.name not in service._jobs and path.name not in service._job_logs
    assert client.get(f"/api/status/{path.name}").json["status"] == "completed"
    assert [e["message"] for e in client.get(f"/api/logs/{path.name}").json["logs"]] == ["saved log", "completion"]


def test_resource_samples_are_bounded_metadata(tmp_path, monkeypatch):
    (tmp_path / "memory.current").write_text("123456")
    (tmp_path / "memory.max").write_text("max")
    (tmp_path / "memory.stat").write_text("anon 42\nfile 43\n")
    stats = diagnostics.sample(tmp_path, tmp_path)
    assert stats == {"cgroup_current_bytes": 123456, "cgroup_anon_bytes": 42, "cgroup_file_bytes": 43}
    monkeypatch.setattr(diagnostics, "sample", lambda: stats)
    log = Mock()
    monkeypatch.setattr(diagnostics._logger, "info", log)
    diagnostics.record("finished", "generation", active_jobs=2)
    assert "123456" in log.call_args[0][0]
    assert "active_jobs" in log.call_args[0][0]
    diagnostics.record("raw-secret", "generation")
    assert log.call_count == 1
    monkeypatch.setattr(diagnostics, "sample", lambda: (_ for _ in ()).throw(OSError("unavailable")))
    diagnostics.record("finished", "edit")  # Memory probing errors must not fail a job.
