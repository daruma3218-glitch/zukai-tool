import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from migration_entry import MigrationGate, create_app


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.calls = []
        self.statuses = []

    def tearDown(self):
        self.tmp.cleanup()

    def app(self, environ, start):
        self.calls.append((environ["REQUEST_METHOD"], environ["PATH_INFO"]))
        start("200 OK", [])
        return [b"application"]

    def request(self, gate, method, path):
        return b"".join(gate({"REQUEST_METHOD": method, "PATH_INFO": path}, lambda status, headers: self.statuses.append(status)))

    def test_all_mutations_are_blocked_before_application(self):
        gate = MigrationGate(self.app, self.root)
        for method, path in [("POST", "/start"), ("POST", "/api/regenerate/example/1"), ("POST", "/api/scene-fix"), ("POST", "/api/resume/example"), ("DELETE", "/api/jobs/example"), ("PATCH", "/future-write-route")]:
            body = self.request(gate, method, path)
            self.assertIn(b"migration_readonly", body)
            self.assertEqual(self.statuses[-1], "503 Service Unavailable")
        self.assertEqual(self.calls, [])

    def test_read_and_login_requests_keep_normal_authentication(self):
        gate = MigrationGate(self.app, self.root)
        for method, path in [("GET", "/"), ("GET", "/download/example"), ("HEAD", "/version"), ("POST", "/login")]:
            self.assertEqual(self.request(gate, method, path), b"application")
        self.assertEqual(len(self.calls), 4)
        self.assertIn(b"migration_readonly", self.request(gate, "DELETE", "/login"))

    def test_marker_stops_an_active_service_without_restart(self):
        gate = MigrationGate(self.app, self.root, "active")
        self.assertEqual(self.request(gate, "POST", "/start"), b"application")
        marker = self.root / ".migration-readonly"
        marker.touch()
        self.assertIn(b"migration_readonly", self.request(gate, "POST", "/start"))
        marker.unlink()
        self.assertEqual(self.request(gate, "POST", "/start"), b"application")

    def test_invalid_or_missing_storage_fails_closed(self):
        gate = MigrationGate(self.app, self.root / "absent", "active")
        self.assertIn(b"migration_readonly", self.request(gate, "POST", "/start"))
        with self.assertRaises(ValueError):
            MigrationGate(self.app, self.root, "typo")
        with patch.object(Path, "stat", side_effect=PermissionError):
            self.assertTrue(MigrationGate(self.app, self.root, "active").read_only())

    def test_empty_credentials_cannot_turn_off_auth(self):
        with patch.dict("os.environ", {"APP_PASSWORD": "", "SECRET_KEY": "", "DATA_DIR": str(self.root)}, clear=True):
            with self.assertRaises(RuntimeError):
                create_app()


if __name__ == "__main__":
    unittest.main()
