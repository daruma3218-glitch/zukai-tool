#!/usr/bin/env python3
"""生成物の保存期間: 古いジョブを自動で削除する（2026-09-24）。

Render の永続ディスク（DATA_DIR / /data）に保存すると再起動・更新では消えなくなる一方、
画像はジョブ1件で約80〜180MB 増えるため、放置すると容量が尽きて生成が止まる。

- 完了・エラーで終わったジョブを、最後の更新から ZUKAI_RETENTION_DAYS 日（既定30日）で削除する。0 以下で無効。
- ディスク使用量が ZUKAI_RETENTION_MAX_USAGE（既定0.80）を超えたら、終わったジョブを古い順に
  使用量が「上限−0.10」になるまで削除する。
- ☆で採用した画像（adoption.json）は消さない。採用のあるジョブは、採用していない画像だけを消し、
  採用画像と記録（JSON）を残す（retention_trimmed.json を置き、以後は対象外）。
  採用記録が読めないジョブは、採用画像を誤って消さないよう丸ごと保留する。
- 採用画像だけで容量の上限を超えている場合は消さずに警告を残し、画面に知らせる（retention_state.json）。
- 実行中・待機中のジョブは消さない（3日以上更新が止まったものは終わったものとして扱う）。
- 削除するのは出力先直下の YYYYMMDD_HHMMSS 形式のフォルダだけ。リンクはたどらない。
- 削除は出力先の retention_log.jsonl に記録する。
"""

import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

JOB_ID_RE = re.compile(r"^\d{8}_\d{6}$")
TERMINAL_STATUSES = {"completed", "error"}
STALE_ACTIVE_DAYS = 3
LOCK_NAME = ".retention.lock"
LOCK_STALE_SECONDS = 30 * 60
LOG_NAME = "retention_log.jsonl"
STATE_NAME = "retention_state.json"
ADOPTION_NAME = "adoption.json"
TRIM_MARKER = "retention_trimmed.json"
_SAFE_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:png|jpe?g|webp)$", re.IGNORECASE)


def retention_days() -> int:
    try:
        return int(os.environ.get("ZUKAI_RETENTION_DAYS", "30"))
    except ValueError:
        return 30


def max_usage_ratio() -> float:
    try:
        value = float(os.environ.get("ZUKAI_RETENTION_MAX_USAGE", "0.80"))
    except ValueError:
        value = 0.80
    return min(max(value, 0.2), 0.98)


def _parse_time(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _read_state(job_dir: Path) -> dict:
    try:
        data = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def last_update(job_dir: Path) -> datetime:
    """job.json の updated_at。無ければ主要ファイルとフォルダの更新時刻の最大値。"""
    parsed = _parse_time(_read_state(job_dir).get("updated_at"))
    if parsed:
        return parsed
    stamps = []
    for path in (job_dir, job_dir / "job.json", job_dir / "manifest.json"):
        try:
            stamps.append(path.stat().st_mtime)
        except OSError:
            pass
    return datetime.fromtimestamp(max(stamps)) if stamps else datetime.now()


def is_finished(job_dir: Path, now: Optional[datetime] = None) -> bool:
    """終わったジョブか。実行中・待機中でも、3日以上更新が止まっていれば終わったものとみなす。"""
    now = now or datetime.now()
    status = _read_state(job_dir).get("status")
    if status in TERMINAL_STATUSES:
        return True
    return now - last_update(job_dir) >= timedelta(days=STALE_ACTIVE_DAYS)


def adopted_files(job_dir: Path) -> tuple:
    """(採用した画像のファイル名の集合, 採用記録を読めたか)。記録が無ければ (空, True)。"""
    path = Path(job_dir) / ADOPTION_NAME
    if not path.exists():
        return set(), True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set(), False
    table = data.get("adopted") if isinstance(data, dict) else None
    if not isinstance(table, dict):
        return set(), False
    keep = {name for name in table if isinstance(name, str) and _SAFE_IMAGE_RE.match(name)}
    # 手直しした版を採用した場合は、画面で版を元画像にひも付けて出すため元画像も残す
    try:
        edits = json.loads((Path(job_dir) / "edits.json").read_text(encoding="utf-8")).get("edits", [])
    except (OSError, ValueError, AttributeError):
        edits = []
    for edit in edits if isinstance(edits, list) else []:
        if isinstance(edit, dict) and edit.get("output") in keep:
            source = edit.get("source")
            if isinstance(source, str) and _SAFE_IMAGE_RE.match(source):
                keep.add(source)
    for name in list(keep):
        match = re.match(r"^(.+?)__e\d+(\.[A-Za-z]+)$", name)
        if match:
            keep.add(match.group(1) + match.group(2))
    return keep, True


def is_trimmed(job_dir: Path) -> bool:
    """保存期限の整理で、採用画像だけを残したジョブか。"""
    return (Path(job_dir) / TRIM_MARKER).is_file()


def expires_at(job_dir: Path, days: Optional[int] = None) -> Optional[datetime]:
    """自動削除（採用のあるジョブは採用外の画像の削除）の予定日時。無効時・実行中・整理済みは None。"""
    days = retention_days() if days is None else days
    if days <= 0 or is_trimmed(job_dir) or not is_finished(job_dir):
        return None
    return last_update(job_dir) + timedelta(days=days)


def load_state(output_dir: Path) -> dict:
    """直近の自動削除の要約（画面の知らせ用）。"""
    try:
        data = json.loads((Path(output_dir) / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _job_dirs(output_dir: Path) -> list:
    if not output_dir.is_dir():
        return []
    root = output_dir.resolve()
    found = []
    for path in output_dir.iterdir():
        try:
            if (JOB_ID_RE.match(path.name) and path.is_dir() and not path.is_symlink()
                    and path.resolve().parent == root):
                found.append(path)
        except OSError:
            continue
    return found


def _dir_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _acquire_lock(output_dir: Path) -> Optional[Path]:
    lock = output_dir / LOCK_NAME
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > LOCK_STALE_SECONDS:
            lock.unlink(missing_ok=True)
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        return lock
    except FileExistsError:
        return None
    except OSError:
        return None


def _log(output_dir: Path, entry: dict) -> None:
    try:
        with open(output_dir / LOG_NAME, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def run_retention(output_dir: Path, *, now: Optional[datetime] = None, days: Optional[int] = None,
                  max_usage: Optional[float] = None,
                  usage_fn: Callable = shutil.disk_usage, dry_run: bool = False) -> dict:
    """期限切れ・容量超過のジョブを整理し、結果の要約を返す（採用画像は消さない）。"""
    output_dir = Path(output_dir)
    now = now or datetime.now()
    days = retention_days() if days is None else days
    max_usage = max_usage_ratio() if max_usage is None else max_usage
    summary = {"checked": 0, "deleted": [], "trimmed": [], "kept_unreadable": [], "skipped_locked": False,
               "dry_run": dry_run, "capacity_triggered": False, "warning": None}
    if not output_dir.is_dir():
        return summary
    lock = _acquire_lock(output_dir)
    if lock is None:
        summary["skipped_locked"] = True
        return summary
    try:
        jobs = _job_dirs(output_dir)
        summary["checked"] = len(jobs)
        finished = [(last_update(job), job) for job in jobs if is_finished(job, now)]
        finished.sort(key=lambda pair: pair[0])

        def record(kind: str, job: Path, updated: datetime, reason: str, size: int, **extra) -> None:
            entry = {"at": now.isoformat(timespec="seconds"), "job_id": job.name, "action": kind,
                     "reason": reason, "bytes": size, "last_update": updated.isoformat(timespec="seconds"),
                     "dry_run": dry_run, **extra}
            summary[kind].append(entry)
            _log(output_dir, entry)

        def delete(job: Path, updated: datetime, reason: str) -> int:
            size = _dir_bytes(job)
            if not dry_run:
                shutil.rmtree(job, ignore_errors=True)
                if job.exists():
                    return 0
            record("deleted", job, updated, reason, size)
            return size

        def trim(job: Path, updated: datetime, reason: str, keep: set) -> int:
            """採用していない画像だけを消し、採用画像と記録を残す。"""
            images = job / "images"
            freed = removed = 0
            if images.is_dir() and not images.is_symlink():
                for path in images.iterdir():
                    if path.name in keep:
                        continue
                    try:
                        if path.is_dir() and not path.is_symlink():
                            size = _dir_bytes(path)
                            if not dry_run:
                                shutil.rmtree(path, ignore_errors=True)
                        else:
                            size = path.lstat().st_size
                            if not dry_run:
                                path.unlink()
                    except OSError:
                        continue
                    freed += size
                    removed += 1
            if not dry_run:
                marker = {"at": now.isoformat(timespec="seconds"), "reason": reason,
                          "kept": sorted(keep), "removed_files": removed, "bytes": freed}
                try:
                    (job / TRIM_MARKER).write_text(json.dumps(marker, ensure_ascii=False, indent=2),
                                                   encoding="utf-8")
                except OSError:
                    pass
            record("trimmed", job, updated, reason, freed, kept=len(keep), removed_files=removed)
            return freed

        def dispose(job: Path, updated: datetime, reason: str) -> int:
            if is_trimmed(job):
                return 0  # 採用画像だけが残っている
            keep, readable = adopted_files(job)
            if not readable:
                if job.name not in summary["kept_unreadable"]:
                    summary["kept_unreadable"].append(job.name)
                    _log(output_dir, {"at": now.isoformat(timespec="seconds"), "job_id": job.name,
                                      "action": "kept", "reason": "採用記録を読めないため保留", "dry_run": dry_run})
                return 0
            if keep:
                return trim(job, updated, reason, keep)
            return delete(job, updated, reason)

        remaining = []
        for updated, job in finished:
            if days > 0 and now - updated >= timedelta(days=days):
                dispose(job, updated, f"{days}日を過ぎた")
            else:
                remaining.append((updated, job))

        try:
            usage = usage_fn(str(output_dir))
            total, used = usage.total, usage.used
        except OSError:
            total = used = 0
        if total and used / total > max_usage:
            summary["capacity_triggered"] = True
            target = max_usage - 0.10
            for updated, job in remaining:
                if used / total <= target:
                    break
                used -= dispose(job, updated, f"容量が{int(max_usage * 100)}%を超えた")
            if used / total > max_usage:
                summary["warning"] = (f"保存領域の{int(used / total * 100)}%を使っています。採用した画像や"
                                      "実行中の結果は自動では消さないため、不要な結果の整理かディスクの増量が必要です。")
                _log(output_dir, {"at": now.isoformat(timespec="seconds"), "action": "warning",
                                  "reason": summary["warning"], "dry_run": dry_run})
        summary["usage_ratio"] = round(used / total, 4) if total else None
        if not dry_run:
            state = {"at": now.isoformat(timespec="seconds"), "usage_ratio": summary["usage_ratio"],
                     "deleted": len(summary["deleted"]), "trimmed": len(summary["trimmed"]),
                     "capacity_triggered": summary["capacity_triggered"], "warning": summary["warning"]}
            try:
                (output_dir / STATE_NAME).write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return summary
    finally:
        lock.unlink(missing_ok=True)


_scheduler_started = False
_scheduler_lock = threading.Lock()


def start_scheduler(output_dir: Path, interval_hours: float = 6, initial_delay: float = 60) -> bool:
    """起動後に1回、その後は一定間隔で自動削除を走らせる（プロセスごとに1本だけ）。"""
    global _scheduler_started
    if os.environ.get("ZUKAI_RETENTION_SCHEDULER", "on").lower() == "off" or "PYTEST_CURRENT_TEST" in os.environ:
        return False
    with _scheduler_lock:
        if _scheduler_started:
            return False
        _scheduler_started = True

    def loop():
        time.sleep(initial_delay)
        while True:
            try:
                run_retention(output_dir)
            except Exception as exc:  # 自動削除の失敗で本体を止めない。記録だけ残す。
                print(f"[retention] error: {exc}", flush=True)
            time.sleep(interval_hours * 3600)

    threading.Thread(target=loop, name="zukai-retention", daemon=True).start()
    return True


def run_in_background(output_dir: Path) -> None:
    """ジョブ完了時など、処理を待たせずに1回だけ走らせる。"""
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    threading.Thread(target=lambda: run_retention(output_dir), name="zukai-retention-once",
                     daemon=True).start()
