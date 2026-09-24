"""Gunicorn entry point that keeps a restored service read-only until cutover.

Use `gunicorn 'migration_entry:create_app()'` with this directory and the pinned source
checkout on PYTHONPATH. Existing source files and stored jobs are unchanged.
"""
import importlib
import json
import os
from pathlib import Path


class MigrationGate:
    def __init__(self, application, data_dir, mode="readonly"):
        if mode not in {"readonly", "active"}:
            raise ValueError("MIGRATION_ACCESS must be readonly or active")
        self.application = application
        self.data_dir = Path(data_dir)
        self.mode = mode

    def read_only(self):
        if self.mode == "readonly":
            return True
        try:
            # stat (rather than exists) distinguishes an unavailable disk from
            # an intentionally absent marker. Disk errors fail closed.
            if not self.data_dir.is_dir():
                return True
            (self.data_dir / ".migration-readonly").stat()
        except FileNotFoundError:
            return False
        except OSError:
            return True
        return True

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "")
        login = method == "POST" and path == "/login"
        if method not in {"GET", "HEAD", "OPTIONS"} and not login and self.read_only():
            body = json.dumps({"ok": False, "code": "migration_readonly", "error": "移行確認中のため、新規生成・再生成・変更は一時停止しています。保存済みの結果は閲覧・ダウンロードできます。"}, ensure_ascii=False).encode("utf-8")
            start_response("503 Service Unavailable", [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body))), ("Cache-Control", "no-store"), ("Retry-After", "300")])
            return [body]
        return self.application(environ, start_response)


def create_app():
    for key in ("APP_PASSWORD", "SECRET_KEY", "DATA_DIR"):
        if not os.environ.get(key, "").strip():
            raise RuntimeError(f"Migration entry requires {key}")
    root = Path(os.environ["DATA_DIR"])
    if not root.is_dir():
        raise RuntimeError("Restored DATA_DIR must exist before application startup")
    mode = os.environ.get("MIGRATION_ACCESS", "readonly")
    if mode not in {"readonly", "active"}:
        raise RuntimeError("Invalid MIGRATION_ACCESS")
    module = importlib.import_module("app")
    from deploy_guard import install
    return MigrationGate(install(module, root), root, mode)


# Gunicorn's factory syntax migration_entry:create_app() avoids import-time
# side effects when the gate is unit tested.
