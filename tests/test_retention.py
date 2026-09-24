"""保存期間（自動削除）の検証。消すべきものと、消してはいけないものの両方を固定する。"""
from collections import namedtuple
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import retention

NOW = datetime(2026, 10, 30, 12, 0, 0)
Usage = namedtuple("Usage", "total used free")


def job(root: Path, name: str, status: str, days_ago: float, size: int = 100) -> Path:
    path = root / name
    (path / "images").mkdir(parents=True)
    (path / "images" / "diagram_001.png").write_bytes(b"x" * size)
    updated = (NOW - timedelta(days=days_ago)).isoformat()
    (path / "job.json").write_text(json.dumps({"status": status, "updated_at": updated}), encoding="utf-8")
    return path


def roomy(_path):
    return Usage(1000, 100, 900)


def test_deletes_finished_jobs_older_than_30_days(tmp_path):
    old = job(tmp_path, "20260901_100000", "completed", 31)
    err = job(tmp_path, "20260902_100000", "error", 45)
    recent = job(tmp_path, "20261001_100000", "completed", 29)
    summary = retention.run_retention(tmp_path, now=NOW, days=30, usage_fn=roomy)
    assert not old.exists() and not err.exists()
    assert recent.exists()
    assert sorted(d["job_id"] for d in summary["deleted"]) == ["20260901_100000", "20260902_100000"]
    log = [json.loads(line) for line in (tmp_path / "retention_log.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {entry["job_id"] for entry in log} == {"20260901_100000", "20260902_100000"}


def test_never_deletes_active_jobs_even_under_capacity_pressure(tmp_path):
    running = job(tmp_path, "20261029_100000", "running", 1)
    queued = job(tmp_path, "20261030_090000", "queued", 0.1)
    summary = retention.run_retention(tmp_path, now=NOW, days=30,
                                      usage_fn=lambda _p: Usage(300, 290, 10))
    assert running.exists() and queued.exists()
    assert summary["deleted"] == []


def test_stale_running_job_is_treated_as_finished_after_retention(tmp_path):
    stale = job(tmp_path, "20260901_000000", "running", 40)
    assert retention.is_finished(stale, NOW)
    retention.run_retention(tmp_path, now=NOW, days=30, usage_fn=roomy)
    assert not stale.exists()


def test_only_job_id_folders_are_ever_deleted(tmp_path):
    keep = [job(tmp_path, name, "completed", 90) for name in ("notes", "20260101_000000_x", "backup")]
    (tmp_path / "retention_log.jsonl").write_text("", encoding="utf-8")
    retention.run_retention(tmp_path, now=NOW, days=30, usage_fn=roomy)
    assert all(path.exists() for path in keep)
    assert (tmp_path / "retention_log.jsonl").exists()


def test_symlinked_job_folder_is_not_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    root = tmp_path / "output"
    root.mkdir()
    try:
        os.symlink(outside, root / "20260101_000000", target_is_directory=True)
    except (OSError, NotImplementedError):
        return  # シンボリックリンクを作れない環境では検証しない
    retention.run_retention(root, now=NOW, days=30, usage_fn=roomy)
    assert (outside / "keep.txt").exists()


def test_capacity_deletes_oldest_finished_until_target(tmp_path):
    oldest = job(tmp_path, "20261001_000000", "completed", 20, size=100)
    middle = job(tmp_path, "20261005_000000", "completed", 15, size=100)
    newest = job(tmp_path, "20261010_000000", "completed", 10, size=100)
    active = job(tmp_path, "20261030_110000", "running", 0.01, size=100)
    # 使用率 90% → 上限80%を超過。目標70%まで古い順に消す（1件=100バイトで80%→… と減る想定）
    summary = retention.run_retention(tmp_path, now=NOW, days=30, max_usage=0.80,
                                      usage_fn=lambda _p: Usage(1000, 900, 100))
    deleted = [d["job_id"] for d in summary["deleted"]]
    assert deleted[0] == "20261001_000000"
    assert not oldest.exists()
    assert active.exists()
    assert summary["usage_ratio"] <= 0.70 or not newest.exists()


def test_retention_disabled_with_zero_days(tmp_path):
    old = job(tmp_path, "20250101_000000", "completed", 400)
    retention.run_retention(tmp_path, now=NOW, days=0, usage_fn=roomy)
    assert old.exists()


def test_fresh_lock_skips_and_stale_lock_is_recovered(tmp_path):
    old = job(tmp_path, "20250101_000000", "completed", 400)
    lock = tmp_path / retention.LOCK_NAME
    lock.write_text("123", encoding="utf-8")
    summary = retention.run_retention(tmp_path, now=NOW, days=30, usage_fn=roomy)
    assert summary["skipped_locked"] is True and old.exists()
    stale = time.time() - retention.LOCK_STALE_SECONDS - 60
    os.utime(lock, (stale, stale))
    retention.run_retention(tmp_path, now=NOW, days=30, usage_fn=roomy)
    assert not old.exists() and not lock.exists()


def test_dry_run_reports_but_keeps(tmp_path):
    old = job(tmp_path, "20250101_000000", "completed", 400)
    summary = retention.run_retention(tmp_path, now=NOW, days=30, usage_fn=roomy, dry_run=True)
    assert old.exists()
    assert summary["deleted"][0]["dry_run"] is True


def test_expires_at_for_finished_and_none_for_active(tmp_path, monkeypatch):
    monkeypatch.setenv("ZUKAI_RETENTION_DAYS", "30")
    done = job(tmp_path, "20261001_000000", "completed", 0)
    running = job(tmp_path, "20261002_000000", "running", 0)
    expected = retention.last_update(done) + timedelta(days=30)
    assert retention.expires_at(done) == expected
    assert retention.expires_at(running) is None
    monkeypatch.setenv("ZUKAI_RETENTION_DAYS", "0")
    assert retention.expires_at(done) is None


def test_scheduler_does_not_start_under_pytest(tmp_path):
    assert retention.start_scheduler(tmp_path) is False
