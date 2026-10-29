"""In-flight relay outages must not discard a late subscription result."""
import tempfile
import unittest
from unittest import mock

import subscription_runtime as rt


class GatewayDisconnectTests(unittest.TestCase):
    def run_gateway(self, *, result_at=None, recover_at=None, invalid_result=False,
                    result_error=False, cancel_at=None):
        clock = [0.0]
        writes = []
        events = []
        notices = []
        good = {"ok": True, "text": "saved result", "payload": {
            "_authentication": "api_key" if invalid_result else "subscription",
            "_provider": "codex", "_routing_version": rt.ROUTING_VERSION}}

        def storage(method, path, body=None):
            if method == "POST":
                writes.append(path)
                return 201, {}
            if method == "DELETE":
                writes.append(path)
                return 200, {}
            if path == "hb/worker.json":
                alive = clock[0] == 0 or (recover_at is not None and clock[0] >= recover_at)
                return 200, {"ts": 1000 + clock[0] if alive else 0,
                             "policy_version": rt.POLICY_VERSION,
                             "routing_version": rt.ROUTING_VERSION}
            if result_at is not None and clock[0] >= result_at:
                return 200, good
            if result_error:
                raise TimeoutError("relay read failed")
            return 404, {}

        def wait(seconds, check):
            clock[0] += seconds
            if check:
                check()

        def cancel():
            if cancel_at is not None and clock[0] >= cancel_at:
                raise rt.RequestCancelled("user stop")

        request = {"id": "same-request", "tool": "zukai", "job_id": "job", "label": "design",
                   "primary": "codex", "use_search": False, "timeout": 30}
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.dict(rt.os.environ, {"SUBSK_STATE_DIR": folder}), \
                mock.patch.object(rt.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(rt.time, "time", side_effect=lambda: 1000 + clock[0]), \
                mock.patch.object(rt, "_cancel_wait", side_effect=wait), \
                mock.patch.object(rt, "_storage", side_effect=storage), \
                mock.patch.object(rt, "_record", side_effect=lambda *a, **kw: events.append(kw)), \
                mock.patch.object(rt, "enqueue_notice", side_effect=lambda *a, **kw: notices.append(kw)):
            try:
                value = rt._gateway_generate(request, cancel_check=cancel if cancel_at else None)
            except (rt.SubscriptionUnavailable, rt.RequestCancelled) as exc:
                value = exc
        return value, clock[0], writes, events, notices

    def test_209_second_outage_recovers_and_accepts_late_result_once(self):
        value, elapsed, writes, events, notices = self.run_gateway(result_at=399, recover_at=210)
        self.assertEqual(value[0], "saved result")
        self.assertEqual(writes, ["req/same-request.json"])
        self.assertTrue(any(e["outcome"] == "worker_connection_recovered" for e in events))
        self.assertFalse(notices)

    def test_result_wins_even_when_heartbeat_remains_stale(self):
        value, elapsed, writes, events, notices = self.run_gateway(result_at=330)
        self.assertEqual(value[0], "saved result")
        self.assertFalse(notices)

    def test_permanent_outage_is_bounded_and_keeps_failure_evidence(self):
        for result_error in (False, True):
            with self.subTest(result_error=result_error):
                value, elapsed, writes, events, notices = self.run_gateway(result_error=result_error)
                self.assertIsInstance(value, rt.SubscriptionUnavailable)
                self.assertEqual(value.reason, "worker_disconnected")
                self.assertGreaterEqual(elapsed, rt.WORKER_DISCONNECT_GRACE_SEC)
                self.assertLess(elapsed, rt.WORKER_DISCONNECT_GRACE_SEC + 70)
                self.assertTrue(value.attempts[-1]["heartbeat"].startswith("age"))
                self.assertEqual(writes, ["req/same-request.json"])
                self.assertEqual(len(notices), 1)

    def test_user_cancel_remains_immediate_and_silent(self):
        value, elapsed, writes, events, notices = self.run_gateway(cancel_at=120)
        self.assertIsInstance(value, rt.RequestCancelled)
        self.assertEqual(elapsed, 120)
        self.assertEqual(writes, ["req/same-request.json", "req/same-request.json"])
        self.assertFalse(notices)

    def test_late_result_still_requires_subscription_authentication(self):
        value, elapsed, writes, events, notices = self.run_gateway(result_at=210, invalid_result=True)
        self.assertIsInstance(value, rt.SubscriptionUnavailable)
        self.assertEqual(value.reason, "billing_route_rejected")


if __name__ == "__main__":
    unittest.main()
