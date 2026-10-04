from datetime import datetime
import threading
import time

import pytest

import app as service
import job_health as health
from test_job_cancellation import client
from utils import save_json

NOW = datetime(2026, 10, 4, 12, 10).timestamp()


def state(**kwargs):
    return dict({"status": "running", "phase": 3, "updated_at": "2026-10-04T12:00:00"}, **kwargs)


def test_threshold_new_progress_and_terminal(tmp_path):
    assert not health.wait_info(tmp_path, state(), True, NOW - 421)["show"]
    info = health.wait_info(tmp_path, state(), True, NOW)
    assert info["code"] == "image_wait" and info["seconds_since_progress"] == 600
    save_json(tmp_path / "images_progress.json", {"updated_at": "2026-10-04T12:09:50"})
    assert not health.wait_info(tmp_path, state(), True, NOW)["show"]
    assert not health.wait_info(tmp_path, state(status="cancelled"), False, NOW)["show"]


def test_orphan_and_cancelling_guidance_never_changes_files(tmp_path):
    save_json(tmp_path / "job.json", state())
    before = (tmp_path / "job.json").read_bytes()
    assert health.wait_info(tmp_path, state(), False, NOW)["code"] == "interrupted"
    assert health.wait_info(tmp_path, state(status="cancelling"), True, NOW)["code"] == "cancelling"
    assert (tmp_path / "job.json").read_bytes() == before
    assert not (tmp_path / "cancel_requested.json").exists()


@pytest.mark.parametrize("worker", ["online", "unconfirmed", "checking"])
def test_analysis_checks_pc_without_claiming_processing_has_stopped(tmp_path, monkeypatch, worker):
    monkeypatch.setattr(health.runtime, "_api_enabled_for", lambda *a: False)
    monkeypatch.setattr(health.runtime, "_api_switch_config", lambda: {})
    monkeypatch.setattr(health.runtime, "cli_path", lambda *a: None)
    monkeypatch.setattr(health.runtime, "_gateway_conf", lambda: True)
    monkeypatch.setattr(health.worker_probe, "sample", lambda: worker)
    info = health.wait_info(tmp_path, state(phase=2), True, NOW)
    assert info["code"] == "worker_wait" and info["worker"] == worker
    assert "待つか" in info["message"]


@pytest.mark.parametrize("direct,local", [(True, None), (False, "cli")])
def test_explicit_api_or_local_route_does_not_blame_remote_pc(tmp_path, monkeypatch, direct, local):
    monkeypatch.setattr(health.runtime, "_api_enabled_for", lambda *a: direct)
    monkeypatch.setattr(health.runtime, "_api_switch_config", lambda: {})
    monkeypatch.setattr(health.runtime, "cli_path", lambda *a: local)
    monkeypatch.setattr(health.worker_probe, "sample", lambda: pytest.fail("remote probe is not needed"))
    assert health.wait_info(tmp_path, state(phase=1), True, NOW)["code"] == "processing_wait"


def test_probe_is_nonblocking_and_shared(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def heartbeat():
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return True, "private diagnostics must not escape"
    monkeypatch.setattr(health.runtime, "_worker_heartbeat", heartbeat)
    probe = health.WorkerProbe()
    try:
        assert probe.sample() == "checking"
        assert entered.wait(2)
        assert [probe.sample() for _ in range(10)] == ["checking"] * 10
        assert calls == [1]
    finally:
        release.set()
    deadline = time.monotonic() + 2
    while probe.inflight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert probe.sample() == "online" and calls == [1]


def test_status_exposes_wait_and_real_progress_resets_it(client, tmp_path, monkeypatch):
    path = tmp_path / "20261004_120000_abcdef"
    path.mkdir()
    save_json(path / "job.json", state())
    service._active_jobs.add(path.name)
    wait_info = health.wait_info
    monkeypatch.setattr(health, "wait_info", lambda p, s, a: wait_info(p, s, a, NOW))
    assert client.get(f"/api/status/{path.name}").json["wait"]["show"]
    service._set_job_state(path.name, message="new batch completed")
    assert "last_progress_at" in service._get_job_state(path.name)
    save_json(path / "images_progress.json", {"updated_at": "2026-10-04T12:09:59"})
    assert not client.get(f"/api/status/{path.name}").json["wait"]["show"]
