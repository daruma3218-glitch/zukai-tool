"""Resume only missing files from the saved, expanded image plan."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
import threading

import generator
from job_control import Cancellation, JobCancelled
import retention
from utils import load_json, save_json


class RetryUnavailable(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


@dataclass
class RetryPlan:
    pending: list
    items: list
    settings: dict
    public: dict


def plan(job_dir, state):
    if retention.is_trimmed(job_dir):
        raise RetryUnavailable("retention", "保存期限などで整理済みのため、不足分の再生成はできません。")
    def read(name):
        try:
            data = load_json(job_dir / name, {})
            if not isinstance(data, dict):
                raise ValueError(name)
            return data
        except (ValueError, OSError):
            raise RetryUnavailable("invalid_plan", "保存された設計・設定を確認できません。設定を直して作り直してください。")

    prompts = read("prompts.json").get("items")
    if not isinstance(prompts, list) or not prompts:
        raise RetryUnavailable("no_plan", "画像の設計が保存されていません。「設定を直して作り直す」を使ってください。")
    manifest = read("manifest.json")
    request = read("request.json")
    previous = read("images_progress.json").get("items") or manifest.get("items") or []
    if not isinstance(previous, list):
        raise RetryUnavailable("invalid_plan", "保存された画像一覧を確認できません。")
    previous = {p.get("index"): p for p in previous if isinstance(p, dict) and isinstance(p.get("index"), int)}
    items, pending, indexes, filenames = [], [], set(), set()
    for saved in prompts:
        if not isinstance(saved, dict):
            raise RetryUnavailable("invalid_plan", "保存された画像指示を確認できません。")
        p = dict(saved)
        idx = p.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool) or not 1 <= idx <= 1000 or idx in indexes:
            raise RetryUnavailable("invalid_plan", "保存された画像番号を確認できません。設定を直して作り直してください。")
        filename = p.get("filename") or generator.image_filename(idx)
        if not isinstance(filename, str) or not re.fullmatch(r"diagram_\d{3,4}(?:_[a-c])?\.png", filename) or filename in filenames:
            raise RetryUnavailable("invalid_plan", "保存された画像ファイル名を確認できません。")
        indexes.add(idx)
        filenames.add(filename)
        p["filename"] = filename
        path = job_dir / "images" / filename
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RetryUnavailable("invalid_file", "保存先の画像を確認できません。管理者へお知らせください。")
        item = {**p, **previous.get(idx, {})}
        if path.is_file() and path.stat().st_size > 0:
            item.update(status="ok", success=True, filename=filename, error="")
        else:
            if p.get("render") == "map":
                valid = isinstance(p.get("map_spec"), dict) and bool(p["map_spec"])
            else:
                valid = isinstance(p.get("prompt"), str) and bool(p["prompt"].strip())
            if not valid:
                raise RetryUnavailable("incomplete_plan", "未生成画像の指示が保存されていません。設定を直して作り直してください。")
            pending.append(p)
            item.update(status="pending", success=False, filename=None, error="")
        items.append(item)
    if not pending:
        raise RetryUnavailable("nothing_missing", "すべての画像が保存されています。")
    # Do not substitute a different provider/model when old metadata is incomplete.
    def setting(key):
        return next((data[key] for data in (request, manifest, state)
                     if data.get(key) not in (None, "")), None)
    settings = {key: setting(key) for key in
                ("provider", "openai_model", "openai_quality", "gemini_model", "concurrency")}
    ai_count = sum(p.get("render") != "map" for p in pending)
    if settings["provider"] not in generator.VALID_PROVIDERS:
        raise RetryUnavailable("missing_settings", "生成時の設定が残っていません。設定を直して作り直してください。")
    if ai_count:
        if settings["provider"] == generator.PROVIDER_GPT_IMAGE:
            valid = (settings["openai_model"] in generator.VALID_OPENAI_IMAGE_MODELS
                     and settings["openai_quality"] in {"low", "medium", "high"})
        else:
            valid = isinstance(settings["gemini_model"], str) and bool(settings["gemini_model"].strip())
        if not valid:
            raise RetryUnavailable("missing_settings", "生成時のモデル・画質が残っていません。設定を直して作り直してください。")
    concurrency = settings["concurrency"]
    if not isinstance(concurrency, int) or isinstance(concurrency, bool) or not 1 <= concurrency <= 32:
        raise RetryUnavailable("missing_settings", "生成時の並列数を確認できません。設定を直して作り直してください。")
    # Honor the current process-wide memory limit for historical high-concurrency jobs too.
    concurrency = settings["concurrency"] = min(concurrency, generator.IMAGE_TASK_LIMIT)
    revision = {"prompts": prompts, "settings": settings, "missing": [p["index"] for p in pending],
                "updated_at": state.get("updated_at"), "run_id": state.get("run_id"), "status": state.get("status")}
    token = hashlib.sha256(json.dumps(revision, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    model = settings["openai_model"] if settings["provider"] == generator.PROVIDER_GPT_IMAGE else settings["gemini_model"]
    public = {"enabled": True, "count": len(pending), "ai_images": ai_count,
              "maps": len(pending) - ai_count, "kept": len(items) - len(pending),
              "plan_token": token, "model": model if ai_count else "地図データ",
              "quality": settings["openai_quality"] if settings["provider"] == generator.PROVIDER_GPT_IMAGE and ai_count else None,
              "concurrency": concurrency}
    return RetryPlan(pending, items, settings, public)


def generate(job_dir, retry_plan, on_progress):
    token = Cancellation(job_dir)
    token.check()
    items = {item["index"]: dict(item) for item in retry_plan.items}
    lock = threading.RLock()

    def snapshot():
        save_json(job_dir / "images_progress.json", {"items": sorted(items.values(), key=lambda i: i["index"]),
                                                    "updated_at": datetime.now().isoformat()})

    def record(info):
        with lock:
            items[info["index"]].update(info)
            items[info["index"]]["success"] = info.get("status") == "ok"
            snapshot()
            finished = sum(i.get("status") in {"ok", "failed", "cancelled"} for i in items.values())
            try:
                on_progress(3, f"不足分を再生成中: {finished}/{len(items)}枚処理済み",
                            50 + int(49 * finished / max(1, len(items))))
            except JobCancelled:
                pass  # Drain sent image requests before finalizing cancellation.

    snapshot()
    generator.run_parallel_generation(prompts=retry_plan.pending, output_dir=job_dir / "images",
        progress_callback=record, cancel_check=token.check, skip_existing=True, **retry_plan.settings)
    token.check()
    manifest = load_json(job_dir / "manifest.json", {})
    manifest.update(title=manifest.get("title") or load_json(job_dir / "analysis.json", {}).get("title")
                    or load_json(job_dir / "job.json", {}).get("title") or job_dir.name,
                    items=sorted(items.values(), key=lambda i: i["index"]), images_planned=len(items),
                    succeeded=sum(i.get("status") == "ok" for i in items.values()),
                    failed=sum(i.get("status") == "failed" for i in items.values()), cancelled=0,
                    status="completed", completed_at=datetime.now().isoformat())
    save_json(job_dir / "manifest.json", manifest)
    return manifest
