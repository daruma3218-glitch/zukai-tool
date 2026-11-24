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
    def usage(_path):
        remaining = (oldest, middle, newest)
        used = 900 - 100 * sum(not folder.exists() for folder in remaining)
        return Usage(1000, used, 1000 - used)

    summary = retention.run_retention(tmp_path, now=NOW, days=30, max_usage=0.80, usage_fn=usage)
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
    class Clock(datetime):
        @classmethod
        def now(cls):
            return NOW
    # Match the job helper's fixed clock; this test must not age into a stale
    # job simply because the real calendar has advanced past 2026-11-02.
    monkeypatch.setattr(retention, "datetime", Clock)
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


# ===== 採用した画像は消さない（2026-09-24 社長への説明どおり） =====
def adopt(job_dir: Path, *names, edits=None):
    (job_dir / "adoption.json").write_text(json.dumps({"version": 1, "adopted": {
        n: {"group": 1, "at": "2026-09-30T10:00:00"} for n in names}}), encoding="utf-8")
    if edits is not None:
        (job_dir / "edits.json").write_text(json.dumps({"version": 1, "edits": edits}), encoding="utf-8")


def test_expired_job_keeps_adopted_images_and_records(tmp_path):
    old = job(tmp_path, "20260901_000000", "completed", 40)
    for name in ("diagram_002_a.png", "diagram_002_b.png", "diagram_003_a.png", "diagram_003_a__e1.png"):
        (old / "images" / name).write_bytes(b"y" * 50)
    adopt(old, "diagram_002_b.png", "diagram_003_a__e1.png",
          edits=[{"source": "diagram_003_a.png", "output": "diagram_003_a__e1.png", "status": "done"}])
    summary = retention.run_retention(tmp_path, now=NOW, days=30, usage_fn=roomy)
    assert old.is_dir() and summary["deleted"] == []
    kept = sorted(p.name for p in (old / "images").iterdir())
    # 採用2枚＋手直し版の元画像を残し、採用していない画像は消す
    assert kept == ["diagram_002_b.png", "diagram_003_a.png", "diagram_003_a__e1.png"]
    assert (old / "job.json").exists() and (old / "adoption.json").exists()
    assert retention.is_trimmed(old) and retention.expires_at(old) is None
    assert summary["trimmed"][0]["removed_files"] == 2
    # 2回目以降は何もしない（採用画像はいつまでも残す）
    again = retention.run_retention(tmp_path, now=NOW + timedelta(days=400), days=30, usage_fn=roomy)
    assert again["trimmed"] == [] and again["deleted"] == []
    assert sorted(p.name for p in (old / "images").iterdir()) == kept


def test_unreadable_adoption_record_keeps_whole_job(tmp_path):
    old = job(tmp_path, "20260901_000000", "completed", 40)
    (old / "adoption.json").write_text("{壊れた", encoding="utf-8")
    summary = retention.run_retention(tmp_path, now=NOW, days=30, usage_fn=roomy)
    assert old.is_dir() and (old / "images" / "diagram_001.png").exists()
    assert summary["kept_unreadable"] == ["20260901_000000"]


def test_capacity_pressure_trims_adopted_jobs_and_warns_when_only_adopted_remain(tmp_path):
    recent = job(tmp_path, "20261020_000000", "completed", 10)
    (recent / "images" / "diagram_002.png").write_bytes(b"z" * 50)
    adopt(recent, "diagram_002.png")
    summary = retention.run_retention(tmp_path, now=NOW, days=30, max_usage=0.80,
                                      usage_fn=lambda _p: Usage(1000, 950, 50))
    assert [t["job_id"] for t in summary["trimmed"]] == ["20261020_000000"]
    assert sorted(p.name for p in (recent / "images").iterdir()) == ["diagram_002.png"]
    assert summary["capacity_triggered"] is True and "採用した画像" in summary["warning"]
    state = retention.load_state(tmp_path)
    assert state["warning"] == summary["warning"] and state["trimmed"] == 1


def test_dry_run_does_not_trim(tmp_path):
    old = job(tmp_path, "20260901_000000", "completed", 40)
    (old / "images" / "diagram_002.png").write_bytes(b"z" * 50)
    adopt(old, "diagram_002.png")
    summary = retention.run_retention(tmp_path, now=NOW, days=30, usage_fn=roomy, dry_run=True)
    assert len(summary["trimmed"]) == 1
    assert (old / "images" / "diagram_001.png").exists() and not retention.is_trimmed(old)
    assert retention.load_state(tmp_path) == {}


def unavailable(_path):
    raise OSError("disk statistics are unavailable")


def test_unreadable_usage_preserves_last_measurement_until_a_new_read_succeeds(tmp_path):
    active = job(tmp_path, "20261030_110000", "running", 0.01)
    high = retention.run_retention(tmp_path, now=NOW, usage_fn=lambda _: Usage(1000, 900, 100))
    assert high["usage_ratio"] == 0.9 and high["warning"]
    for hour in (1, 2):
        latest = NOW + timedelta(hours=hour)
        result = retention.run_retention(tmp_path, now=latest, usage_fn=unavailable)
        saved = retention.load_state(tmp_path)
        assert result["usage_ratio"] is None and saved["usage_ratio"] is None
        assert saved["at"] == latest.isoformat(timespec="seconds")
        assert saved["last_measurement"] == {"at": NOW.isoformat(timespec="seconds"), "usage_ratio": 0.9}
        assert "確認できません" in result["warning"] and "前回の実測" in result["warning"]
        assert "90.0%" in result["warning"] and "90%を使っています" not in result["warning"]
        assert saved["warning"] == result["warning"] and active.exists()
    healthy = retention.run_retention(tmp_path, now=NOW + timedelta(hours=3), usage_fn=roomy)
    assert healthy["usage_ratio"] == 0.1 and healthy["warning"] is None
    saved = retention.load_state(tmp_path)
    assert saved["warning"] is None and saved["last_measurement"]["usage_ratio"] == 0.1


def test_first_failed_or_zero_capacity_read_is_unknown(tmp_path):
    for probe in (unavailable, lambda _: Usage(0, 0, 0)):
        result = retention.run_retention(tmp_path, now=NOW, usage_fn=probe)
        assert result["usage_ratio"] is None and "確認できません" in result["warning"]
        assert "%" not in result["warning"]
        assert retention.load_state(tmp_path)["last_measurement"] is None


def test_existing_state_reuses_only_measured_usage(tmp_path):
    past = (NOW - timedelta(days=1)).isoformat(timespec="seconds")
    state = tmp_path / retention.STATE_NAME
    for trimmed, expected in ((0, {"at": past, "usage_ratio": 0.86}), (1, None)):
        state.write_text(json.dumps({"at": past, "usage_ratio": 0.86, "capacity_triggered": True,
                                     "deleted": 0, "trimmed": trimmed}), encoding="utf-8")
        retention.run_retention(tmp_path, now=NOW, usage_fn=unavailable)
        assert retention.load_state(tmp_path)["last_measurement"] == expected


def test_cleanup_reports_measured_usage_even_when_files_did_not_free_expected_space(tmp_path):
    finished = job(tmp_path, "20261020_000000", "completed", 10, size=600)
    active = job(tmp_path, "20261030_110000", "running", 0.01)
    result = retention.run_retention(tmp_path, now=NOW, usage_fn=lambda _: Usage(1000, 900, 100))
    assert not finished.exists() and active.exists()
    assert result["usage_ratio"] == 0.9 and "90%" in result["warning"]
    assert retention.load_state(tmp_path)["last_measurement"]["usage_ratio"] == 0.9


def test_failed_post_cleanup_probe_keeps_initial_read_and_reports_uncertainty(tmp_path):
    finished = job(tmp_path, "20261020_000000", "completed", 10, size=600)
    readings = iter((lambda _: Usage(1000, 900, 100), unavailable))
    result = retention.run_retention(tmp_path, now=NOW, usage_fn=lambda path: next(readings)(path))
    assert not finished.exists()
    assert result["capacity_triggered"] and result["usage_ratio"] is None
    assert "確認できません" in result["warning"] and "90.0%" in result["warning"]
