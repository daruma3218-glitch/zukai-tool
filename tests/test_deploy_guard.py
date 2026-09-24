import io
import json
import threading
import time
from unittest.mock import Mock

import pytest

from deploy_guard import CONTROL_PATH, DeployGuard, signature

SHA = "a" * 40


@pytest.fixture
def guard(tmp_path):
    def app(env, start):
        start("200 OK", [])
        return [b"ok"]
    return DeployGuard(app, tmp_path, "test-only", "b" * 40, "old",
                       Mock(return_value=False), hook="test-hook", clock=lambda: 1000,
                       trigger=Mock())


def request(guard, method="POST", path="/start", **extra):
    statuses = []
    body = b"".join(guard({"REQUEST_METHOD": method, "PATH_INFO": path, **extra},
                         lambda status, headers: statuses.append(status)))
    return statuses[0], body


def test_signed_control_rejects_password_session_and_stale_requests(guard):
    body = json.dumps({"target": SHA}).encode()
    for stamp, secret in [(1000, "wrong"), (900, "test-only")]:
        status, _ = request(guard, path=CONTROL_PATH, CONTENT_LENGTH=str(len(body)),
            **{"wsgi.input": io.BytesIO(body), "HTTP_X_DEPLOY_TIME": str(stamp),
               "HTTP_X_DEPLOY_SIGNATURE": signature(secret, stamp, body)})
        assert status == "403 Forbidden"
    assert not guard.file.exists()
    status, result = request(guard, path=CONTROL_PATH, CONTENT_LENGTH=str(len(body)),
        **{"wsgi.input": io.BytesIO(body), "HTTP_X_DEPLOY_TIME": "1000",
           "HTTP_X_DEPLOY_SIGNATURE": signature("test-only", "1000", body)})
    assert status == "200 OK"
    assert json.loads(result)["ready"]


def test_idle_is_sealed_until_matching_replacement(guard):
    assert guard.drain(SHA)["ready"]
    assert request(guard)[0] == "503 Service Unavailable"
    assert request(guard, "GET")[0] == "200 OK"
    assert request(guard, path="/login")[0] == "200 OK"
    # Old worker restart or rollback must not reopen production accidentally.
    for commit, instance in [(SHA, "old"), ("b" * 40, "new")]:
        DeployGuard(guard.application, guard.root, "test-only", commit, instance, lambda: False)
        assert guard.file.exists()
    DeployGuard(guard.application, guard.root, "test-only", SHA, "new", lambda: False)
    assert not guard.file.exists()


def test_generation_waits_without_render_timeout_then_retries_once(guard):
    guard.jobs_busy.return_value = True
    assert not guard.drain(SHA)["ready"]
    guard.clock = lambda: 10000  # Longer than Render's 30-minute pre-deploy limit.
    guard.tick()
    guard.trigger.assert_not_called()
    guard.jobs_busy.return_value = False
    guard.tick()
    guard.tick()
    guard.trigger.assert_called_once_with("test-hook")
    assert guard._read()["phase"] == "sealed"
    assert request(guard)[0] == "503 Service Unavailable"


def test_synchronous_regeneration_and_background_tail_are_protected(guard):
    entered, release = threading.Event(), threading.Event()
    def app(env, start):
        entered.set()
        assert release.wait(5)
        start("200 OK", [])
        return [b"ok"]
    guard.application = app
    worker = threading.Thread(target=lambda: request(guard))
    worker.start()
    assert entered.wait(5)
    assert not guard.drain(SHA)["ready"]
    release.set()
    worker.join(5)
    assert not worker.is_alive()
    entered.clear()
    release.clear()
    @guard.background_task
    def pipeline_tail():
        entered.set()
        assert release.wait(5)
    worker = threading.Thread(target=pipeline_tail)
    worker.start()
    assert entered.wait(5)
    assert not guard.drain(SHA)["ready"]
    release.set()
    worker.join(5)
    assert guard.drain(SHA)["ready"]


def test_job_io_errors_and_invalid_state_fail_closed(guard):
    guard.jobs_busy.side_effect = ValueError("partial JSON write")
    assert not guard.drain(SHA)["ready"]
    guard.file.write_text("{incomplete", encoding="utf-8")
    assert request(guard)[0] == "503 Service Unavailable"
    assert not guard.drain(SHA)["ready"]


def test_failed_hook_is_bounded_and_never_reopens_writes(guard):
    guard.jobs_busy.return_value = True
    guard.drain(SHA)
    guard.jobs_busy.return_value = False
    guard.trigger.side_effect = RuntimeError("sensitive hook must not be logged")
    for now in [1100, 2000, 3000, 4000]:
        guard.clock = lambda: now
        guard.tick()
    assert guard.trigger.call_count == 3
    assert guard._read()["phase"] == "error"
    assert request(guard)[0] == "503 Service Unavailable"


def test_newer_successful_build_replaces_pending_commit(guard):
    guard.jobs_busy.return_value = True
    guard.drain(SHA)
    guard.jobs_busy.return_value = False
    newer = "c" * 40
    assert guard.drain(newer)["ready"]
    assert guard._read()["target"] == newer


def test_iteration_error_releases_request_counter(guard):
    def app(env, start):
        raise RuntimeError("handler failed")
    guard.application = app
    with pytest.raises(RuntimeError):
        request(guard)
    assert guard.writes == 0


def test_predeploy_fails_closed_on_endpoint_error(monkeypatch):
    import urllib.request
    from deploy_guard import predeploy
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://example.onrender.com")
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA)
    monkeypatch.setenv("SECRET_KEY", "test-only")
    opener = Mock()
    opener.open.side_effect = OSError("no network")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: opener)
    with pytest.raises(SystemExit, match="keeping existing instance"):
        predeploy()


@pytest.mark.parametrize("status", [200, 202])
def test_hook_accepts_started_or_queued_without_ref(monkeypatch, status):
    import urllib.request
    from deploy_guard import call_hook
    response = Mock(status=status)
    opener = Mock()
    opener.open.return_value.__enter__ = Mock(return_value=response)
    opener.open.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *args: opener)
    call_hook("https://api.render.com/deploy/srv-test?key=fixture")
    with pytest.raises(ValueError):
        call_hook("https://api.render.com/deploy/srv-test?key=fixture&ref=" + SHA)
