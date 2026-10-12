"""Small, metadata-only memory samples in Render's normal application log."""
import json
import logging
import os
from pathlib import Path


_logger = logging.getLogger("zukai.resources")
if not _logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)
_logger.setLevel(logging.INFO)
_logger.propagate = False


def sample(cgroup=Path("/sys/fs/cgroup"), proc=Path("/proc/self")):
    values = {}
    for name in ("current", "peak", "max"):
        try:
            values[f"cgroup_{name}_bytes"] = int((cgroup / f"memory.{name}").read_text().strip())
        except (OSError, ValueError):
            pass  # Linux's 'max', or a local system without cgroup v2.
    try:
        stats = dict(line.split() for line in (cgroup / "memory.stat").read_text().splitlines())
        for name in ("anon", "file"):
            values[f"cgroup_{name}_bytes"] = int(stats[name])
    except (OSError, ValueError, KeyError):
        pass
    try:
        pages = int((proc / "statm").read_text().split()[1])
        values["rss_bytes"] = pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    return values


def record(event, operation, *, active_jobs=None, recovered_jobs=None, recovered_edits=None):
    """No paths, manuscript, prompts, API responses or environment values."""
    try:
        if event not in {"started", "phase", "finished", "recovered"}:
            return
        if operation not in {"generation", "missing_retry", "edit", "flip", "startup"}:
            return
        counts = {key: int(value) for key, value in {"active_jobs": active_jobs,
                  "recovered_jobs": recovered_jobs, "recovered_edits": recovered_edits}.items() if value is not None}
        _logger.info(json.dumps({"event": f"zukai_resources.{event}", "operation": operation,
                                "pid": os.getpid(), **counts, **sample()}, sort_keys=True))
    except Exception:
        pass  # Diagnostics must never interrupt generation or result saving.
