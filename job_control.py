"""Job-scoped, durable cancellation. No process-wide signals or thread killing."""
import time
from pathlib import Path

from subscription_runtime import RequestCancelled


class JobCancelled(RequestCancelled):
    pass


class Cancellation:
    def __init__(self, job_dir):
        self.path = Path(job_dir) / "cancel_requested.json"

    def requested(self):
        return self.path.exists()

    def check(self):
        if self.requested():
            raise JobCancelled("ジョブを中止しました")


def check_cancel(check=None):
    if check:
        check()


def wait_or_cancel(seconds, check=None):
    if not check:
        time.sleep(seconds)
        return
    until = time.monotonic() + seconds
    while True:
        check()
        remaining = until - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.25, remaining))
