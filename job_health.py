"""Read-only wait guidance. It never stops or retries a job."""
from datetime import datetime
import threading
import time

import subscription_runtime as runtime
from utils import load_json

WAIT_SECONDS = 180


class WorkerProbe:
    """One shared, non-blocking heartbeat check per 30 seconds at most."""
    def __init__(self):
        self.lock = threading.Lock()
        self.inflight = False
        self.checked = None
        self.state = "checking"

    def _check(self):
        try:
            alive, _ = runtime._worker_heartbeat()
            state = "online" if alive else "unconfirmed"
        except Exception:
            state = "unconfirmed"
        with self.lock:
            self.state, self.checked, self.inflight = state, time.monotonic(), False

    def sample(self):
        with self.lock:
            fresh = self.checked is not None and time.monotonic() - self.checked < 30
            if not fresh and not self.inflight:
                self.inflight = True
                try:
                    threading.Thread(target=self._check, daemon=True, name="zukai-wait-health").start()
                except Exception:
                    self.inflight = False
            return self.state if fresh else "checking"


worker_probe = WorkerProbe()


def _time(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def wait_info(job_dir, state, active, now=None):
    if state.get("status") not in {"queued", "running", "cancelling"}:
        return {"show": False}
    now = time.time() if now is None else now
    snapshot = load_json(job_dir / "images_progress.json", {})
    stamps = [_time(state.get("last_progress_at") or state.get("updated_at") or state.get("started_at")),
              _time(snapshot.get("updated_at"))]
    stamps = [s for s in stamps if s is not None and s <= now + 60]
    age = max(0, int(now - max(stamps))) if stamps else None
    if age is not None and age < WAIT_SECONDS:
        return {"show": False, "seconds_since_progress": age}
    phase = state.get("phase", 0)
    stage = "画像づくり" if phase == 3 else "候補の設計" if phase == 2 else "原稿の分析・箇所の抽出"
    info = {"show": True, "seconds_since_progress": age, "stage": stage, "worker": "not_needed"}
    if not active:
        info.update(code="interrupted", message="このサーバーで実行中の処理を確認できません。中止してから、不足分の再生成または設定を直した作り直しを選べます。")
    elif state.get("status") == "cancelling":
        info.update(code="cancelling", message="中止処理に時間がかかっています。送信済みの画像の応答を待つ場合があります。完成画像は保存できます。")
    elif phase == 3:
        info.update(code="image_wait", message="画像の応答に時間がかかっています。処理が続いている場合もあります。待つか、「ジョブ全体を中止」を選べます。")
    else:
        direct = runtime._api_enabled_for({"tool": "zukai"}, runtime._api_switch_config())
        local = runtime.cli_path("claude") or runtime.cli_path("codex")
        if not direct and not local and runtime._gateway_conf():
            info["worker"] = worker_probe.sample()
            messages = {
                "checking": "処理用PCとの接続を確認しています。処理が続いている場合もあります。",
                "online": "処理用PCとの接続を確認できました。ほかの処理の順番待ちや、AIの応答待ちの可能性があります。",
                "unconfirmed": "処理用PCとの接続を確認できません。PCの稼働や通信状況をご確認ください。",
            }
            info.update(code="worker_wait", message=messages[info["worker"]] + " 待つか、「ジョブ全体を中止」を選べます。")
        else:
            info.update(code="processing_wait", message="処理の応答に時間がかかっています。待つか、「ジョブ全体を中止」を選べます。")
    return info
