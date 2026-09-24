"""Drain generation before Render replaces this single-worker service.

Only Render, using the existing server-side SECRET_KEY, can request a drain.
No project data or credentials are returned by the control endpoint.
"""
import functools
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import urllib.request

CONTROL_PATH = "/_ops/deploy/drain"
TERMINAL = {"completed", "error", "failed", "cancelled", "interrupted"}
MESSAGE = "更新を準備しています。実行中の生成が完了するまで待機し、自動で更新します。保存済みの結果は閲覧・ダウンロードできます。"


def signature(secret, timestamp, body):
    message = b"sentence-deploy-v1\n" + str(timestamp).encode() + b"\n" + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def call_hook(url):
    # Never send this credential to a user-selected host or follow redirects.
    from urllib.parse import urlsplit, parse_qs
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "api.render.com"
            or not re.fullmatch(r"/deploy/srv-[a-z0-9]+", parsed.path)
            or set(parse_qs(parsed.query)) != {"key"}):
        raise ValueError("Invalid Render deploy hook configuration")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.build_opener(NoRedirect).open(req, timeout=20) as response:
        if response.status not in {200, 202}:
            raise RuntimeError("Render did not accept the deployment retry")


class DeployGuard:
    def __init__(self, application, root, secret, commit, instance, jobs_busy,
                 hook="", clock=time.time, trigger=call_hook):
        self.application = application
        self.root = Path(root)
        self.file = self.root / ".deploy-guard.json"
        self.secret, self.commit, self.instance = secret, commit, instance
        self.jobs_busy, self.hook = jobs_busy, hook
        self.clock, self.trigger = clock, trigger
        self.lock = threading.RLock()
        self.writes = self.background = 0
        with self.lock:
            state = self._read()
            if (state and state.get("phase") == "sealed"
                    and state.get("target") == commit
                    and state.get("source_instance") != instance):
                self.file.unlink()

    def _read(self):
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("phase") not in {"waiting", "sealed", "error"}:
                raise ValueError("Invalid deployment state")
            return data
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"phase": "error", "reason": "unreadable_state"}

    def _save(self, state):
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, self.file)

    def _busy(self):
        if self.writes or self.background:
            return True
        try:
            return self.jobs_busy()
        except Exception:
            # Corrupt/unavailable job metadata must never be treated as idle.
            return True

    def background_task(self, function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            with self.lock:
                self.background += 1
            try:
                return function(*args, **kwargs)
            finally:
                with self.lock:
                    self.background -= 1
        return wrapped

    def drain(self, target):
        with self.lock:
            state = self._read()
            if state and state.get("reason") == "unreadable_state":
                return {"ready": False, "retry": False, "reason": "unreadable_state"}
            if not state or state.get("target") != target:
                state = {"target": target, "phase": "waiting", "attempts": 0,
                         "source_instance": self.instance, "requested_at": self.clock(),
                         "retry_after": self.clock() + 90}
            ready = not self._busy()
            if ready:
                state["phase"] = "sealed"
            self._save(state)
            return {"ready": ready, "retry": bool(self.hook), "phase": state["phase"]}

    def tick(self):
        with self.lock:
            state = self._read()
            if (not state or state["phase"] != "waiting" or not self.hook
                    or self.clock() < state["retry_after"] or self._busy()):
                return
            # Seal before requesting replacement; never admit a job between
            # the idle check and the platform accepting a deploy.
            state["phase"] = "sealed"
            state["attempts"] += 1
            self._save(state)
        try:
            self.trigger(self.hook)
            logging.info("Generation finished; queued Render deployment retried")
        except Exception:
            # Don't log exception text: network errors can contain hook keys.
            logging.error("Deployment retry failed; existing service remains protected")
            with self.lock:
                current = self._read()
                if current == state:
                    current["phase"] = "waiting" if state["attempts"] < 3 else "error"
                    current["retry_after"] = self.clock() + 120 * state["attempts"]
                    self._save(current)

    def watch(self):
        while True:
            time.sleep(15)
            try:
                self.tick()
            except Exception:
                logging.error("Deployment state check failed; manual inspection required")

    @staticmethod
    def response(start, code, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        start(code, [("Content-Type", "application/json; charset=utf-8"),
                     ("Content-Length", str(len(body))), ("Cache-Control", "no-store"),
                     ("Retry-After", "30")])
        return [body]

    def __call__(self, environ, start):
        method, path = environ.get("REQUEST_METHOD", "GET"), environ.get("PATH_INFO", "")
        if path == CONTROL_PATH:
            try:
                size = int(environ.get("CONTENT_LENGTH", "0"))
                stamp = environ.get("HTTP_X_DEPLOY_TIME", "")
                if method != "POST" or not 0 < size <= 512 or abs(self.clock() - int(stamp)) > 60:
                    raise ValueError()
                body = environ["wsgi.input"].read(size)
                actual = environ.get("HTTP_X_DEPLOY_SIGNATURE", "")
                if not hmac.compare_digest(signature(self.secret, stamp, body), actual):
                    raise ValueError()
                target = json.loads(body)["target"]
                if not isinstance(target, str) or not re.fullmatch(r"[0-9a-f]{40}", target):
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                return self.response(start, "403 Forbidden", {"error": "forbidden"})
            return self.response(start, "200 OK", self.drain(target))
        mutation = method not in {"GET", "HEAD", "OPTIONS"} and not (method == "POST" and path == "/login")
        if not mutation:
            return self.application(environ, start)
        with self.lock:
            if self._read() or not self.root.is_dir():
                return self.response(start, "503 Service Unavailable",
                                     {"ok": False, "code": "deployment_pending", "error": MESSAGE})
            self.writes += 1
        def respond():
            result = None
            try:
                result = self.application(environ, start)
                yield from result
            finally:
                try:
                    if hasattr(result, "close"):
                        result.close()
                finally:
                    with self.lock:
                        self.writes -= 1
        return respond()


def install(module, root):
    def jobs_busy():
        with module._jobs_lock:
            if any(state.get("status") not in TERMINAL for state in module._jobs.values()):
                return True
        for directory in module.OUTPUT_DIR.iterdir():
            if not directory.is_dir():
                continue
            path = directory / "job.json"
            if path.exists() and json.loads(path.read_text(encoding="utf-8")).get("status") not in TERMINAL:
                return True
        return False
    guard = DeployGuard(module.app, root, os.environ["SECRET_KEY"],
                        os.environ.get("RENDER_GIT_COMMIT", "local"),
                        os.environ.get("RENDER_INSTANCE_ID", f"local-{os.getpid()}"),
                        jobs_busy, os.environ.get("RENDER_DEPLOY_HOOK", ""))
    module._run_pipeline_thread = guard.background_task(module._run_pipeline_thread)
    threading.Thread(target=guard.watch, daemon=True, name="deployment-guard").start()
    return guard


def predeploy():
    """Fail this attempt quickly while busy; the running server retries on idle."""
    from urllib.parse import urlsplit
    url = os.environ["RENDER_EXTERNAL_URL"]
    parsed = urlsplit(url)
    # Render supplies this URL. Avoid forwarding a signed control message elsewhere.
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".onrender.com") or parsed.username or parsed.password:
        raise RuntimeError("RENDER_EXTERNAL_URL must be the Render HTTPS hostname")
    body = json.dumps({"target": os.environ["RENDER_GIT_COMMIT"]}).encode()
    stamp = str(int(time.time()))
    request = urllib.request.Request(url.rstrip("/") + CONTROL_PATH, data=body,
        headers={"Content-Type": "application/json", "X-Deploy-Time": stamp,
                 "X-Deploy-Signature": signature(os.environ["SECRET_KEY"], stamp, body)})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            result = json.load(response)
    except Exception:
        raise SystemExit("Deployment guard unavailable; keeping existing instance running") from None
    if result.get("ready") is not True:
        reason = "automatic retry queued after generation" if result.get("retry") else "operator review required"
        raise SystemExit("Deployment postponed: " + reason)
    print("Generation drained; new work blocked until replacement is ready")


if __name__ == "__main__":
    predeploy()
