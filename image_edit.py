#!/usr/bin/env python3
"""1枚ずつの手直しと採用の記録（2026-09-24）。

- 左右反転: AI を使わずその場で作る（費用なし）。
- 文字を全部消す / 指示して直す: OpenAI の画像編集（既定 gpt-image-2.5-sunburst）。1回で画像1枚分の費用。
- 元の画像は上書きしない。直した画像は diagram_012__e1.png のような別ファイル（版）として保存し、
  edits.json に記録する。
- どの画像を使うかは adoption.json に「採用」として記録し、採用分だけ ZIP で取り出せる。

複数のワーカー（gunicorn）から同じジョブの記録を更新しても壊れないよう、記録の読み書きはロックする。
"""

import csv
import io
import json
import os
import re
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

import generator

EDITS_NAME = "edits.json"
ADOPTION_NAME = "adoption.json"
LOCK_TIMEOUT = 10.0
MAX_RUNNING_EDITS_PER_JOB = 3
IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+\.(png|jpg|jpeg|webp)$")
EDIT_SUFFIX_RE = re.compile(r"__e(\d+)$")

REMOVE_TEXT_PROMPT = (
    "Remove every piece of text from this image: letters, words, numbers, labels, captions, "
    "signage text and watermarks, in any language. Do not change anything else. Keep the composition, "
    "objects, people, icons, arrows, colors, lighting and style exactly the same, and fill the areas "
    "where text was removed naturally, as if the text had never been there."
)
INSTRUCT_SUFFIX = (
    "\n\nOnly change what is requested above. Keep everything else in the image exactly the same: "
    "composition, style, colors, other objects and any existing text that is not mentioned."
)
ACTIONS = {"flip", "remove_text", "instruct"}
ACTION_LABELS = {"flip": "左右反転", "remove_text": "文字を全部消す", "instruct": "指示して直す"}


class EditError(ValueError):
    pass


# ===== 記録ファイル（ロック付き） =====
class _JobLock:
    def __init__(self, job_dir: Path, name: str):
        self.path = job_dir / f".{name}.lock"

    def __enter__(self):
        deadline = time.time() + LOCK_TIMEOUT
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 60:
                        self.path.unlink(missing_ok=True)  # 異常終了で残ったロック
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    raise TimeoutError("記録ファイルが使用中です。少し待ってからやり直してください。")
                time.sleep(0.05)

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data) -> None:
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as temp:
            temp_path = Path(temp.name)
            json.dump(data, temp, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _update(job_dir: Path, name: str, default, change):
    with _JobLock(job_dir, name):
        data = _read(job_dir / name, default)
        result = change(data)
        _write(job_dir / name, data)
        return result


def load_edits(job_dir: Path) -> list:
    data = _read(Path(job_dir) / EDITS_NAME, {"version": 1, "edits": []})
    return data.get("edits", []) if isinstance(data, dict) else []


def load_adoption(job_dir: Path) -> dict:
    data = _read(Path(job_dir) / ADOPTION_NAME, {"version": 1, "adopted": {}})
    adopted = data.get("adopted", {}) if isinstance(data, dict) else {}
    return adopted if isinstance(adopted, dict) else {}


# ===== ファイル名 =====
def source_stem(filename: str) -> str:
    """diagram_004_b__e2.png → diagram_004_b（どの元画像の版か）。"""
    return EDIT_SUFFIX_RE.sub("", Path(filename).stem)


def is_edit_file(filename: str) -> bool:
    return bool(EDIT_SUFFIX_RE.search(Path(filename).stem))


def _image_path(job_dir: Path, filename: str) -> Path:
    if not isinstance(filename, str) or not IMAGE_NAME_RE.match(filename):
        raise EditError("画像の指定が不正です")
    path = job_dir / "images" / filename
    if not path.is_file():
        raise EditError("画像が見つかりません")
    return path


def _next_edit_name(job_dir: Path, source: str, edits: list) -> str:
    stem = source_stem(source)
    used = [int(m.group(1)) for e in edits
            if source_stem(e.get("output", "")) == stem and (m := EDIT_SUFFIX_RE.search(Path(e["output"]).stem))]
    for path in (job_dir / "images").glob(f"{stem}__e*.png"):
        match = EDIT_SUFFIX_RE.search(path.stem)
        if match:
            used.append(int(match.group(1)))
    return f"{stem}__e{max(used, default=0) + 1}.png"


# ===== 手直し =====
def request_edit(job_dir: Path, source: str, action: str, instruction: str = "",
                 run_async: bool = True, client_factory=None) -> dict:
    """手直しを受け付け、記録の1件を返す。左右反転はその場で完了、AIの手直しは別スレッドで進める。"""
    job_dir = Path(job_dir)
    if action not in ACTIONS:
        raise EditError("手直しの種類が不正です")
    instruction = (instruction or "").strip()
    if action == "instruct" and not instruction:
        raise EditError("直してほしい内容を入力してください")
    if len(instruction) > 1000:
        raise EditError("指示は1000文字以内にしてください")
    source_path = _image_path(job_dir, source)
    model = "" if action == "flip" else generator.resolve_edit_model()
    if action != "flip" and not os.environ.get("OPENAI_API_KEY") and client_factory is None:
        raise EditError("OPENAI_API_KEY が設定されていないため、AIの手直しは使えません")

    def reserve(data):
        edits = data.setdefault("edits", [])
        running = [e for e in edits if e.get("status") == "running"]
        for e in running:  # 同じ依頼の二重送信は、進行中の1件を返して重複させない
            if e.get("source") == source and e.get("action") == action and e.get("instruction") == instruction:
                return dict(e, duplicate=True)
        if action != "flip" and len(running) >= MAX_RUNNING_EDITS_PER_JOB:
            raise EditError(f"同時に進められる手直しは{MAX_RUNNING_EDITS_PER_JOB}件までです。終わるまでお待ちください")
        entry = {"id": uuid.uuid4().hex[:12], "source": source,
                 "output": _next_edit_name(job_dir, source, edits), "action": action,
                 "label": ACTION_LABELS[action], "instruction": instruction, "model": model,
                 "status": "running", "error": "", "created_at": datetime.now().isoformat(timespec="seconds")}
        edits.append(entry)
        return dict(entry)

    entry = _update(job_dir, EDITS_NAME, {"version": 1, "edits": []}, reserve)
    if entry.get("duplicate"):
        return entry
    output_path = job_dir / "images" / entry["output"]

    def finish(ok: bool, error: str = ""):
        def change(data):
            for e in data.get("edits", []):
                if e.get("id") == entry["id"]:
                    e.update(status="ok" if ok else "failed", error=error[:200],
                             finished_at=datetime.now().isoformat(timespec="seconds"))
                    return dict(e)
            return None
        return _update(job_dir, EDITS_NAME, {"version": 1, "edits": []}, change)

    if action == "flip":
        try:
            with Image.open(source_path) as img:
                generator._save_png(ImageOps.mirror(img.convert("RGB")), output_path)
            return finish(True)
        except Exception as exc:
            return finish(False, f"左右反転に失敗: {exc}")

    prompt = REMOVE_TEXT_PROMPT if action == "remove_text" else instruction + INSTRUCT_SUFFIX

    def work():
        try:
            if client_factory is not None:
                client = client_factory()
            else:
                import openai
                client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            ok, error = generator._sync_edit_image_openai(client, source_path, prompt, output_path,
                                                          model_name=model)
        except Exception as exc:
            ok, error = False, str(exc)
        finish(ok, error)

    if run_async:
        threading.Thread(target=work, name=f"zukai-edit-{entry['id']}", daemon=True).start()
        return entry
    work()
    return next((e for e in load_edits(job_dir) if e.get("id") == entry["id"]), entry)


# ===== 採用 =====
def set_adopted(job_dir: Path, filename: str, adopted: bool, group=None) -> dict:
    job_dir = Path(job_dir)
    _image_path(job_dir, filename)

    def change(data):
        table = data.setdefault("adopted", {})
        if adopted:
            table[filename] = {"group": group, "at": datetime.now().isoformat(timespec="seconds")}
        else:
            table.pop(filename, None)
        return dict(table)

    return _update(job_dir, ADOPTION_NAME, {"version": 1, "adopted": {}}, change)


def adopted_zip(job_dir: Path, items: list, title: str) -> tuple:
    """採用した画像だけの ZIP（一時ファイル）と中身の枚数。対応表 CSV を同梱する。"""
    job_dir = Path(job_dir)
    adopted = load_adoption(job_dir)
    by_stem = {}
    for item in items:
        if item.get("filename"):
            by_stem[Path(item["filename"]).stem] = item
    edits = {e.get("output"): e for e in load_edits(job_dir)}
    rows = []
    archive = tempfile.TemporaryFile(mode="w+b")
    count = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
        for filename in sorted(adopted):
            try:
                path = _image_path(job_dir, filename)
            except EditError:
                continue  # 採用後に消えた画像は飛ばす
            zf.write(path, f"images/{filename}")
            count += 1
            item = by_stem.get(source_stem(filename), {})
            edit = edits.get(filename, {})
            rows.append([item.get("group") or item.get("index") or "", filename, item.get("variant", ""),
                         item.get("variant_label", ""), edit.get("label", ""), item.get("section", ""),
                         item.get("excerpt", "")])
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["箇所番号", "ファイル名", "案", "種類", "手直し", "章", "原稿の抜粋"])
        writer.writerows(rows)
        zf.writestr("採用一覧.csv", "﻿" + buffer.getvalue(), compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("README.txt", f"{title}\n採用した画像 {count} 枚。対応表は 採用一覧.csv。\n",
                    compress_type=zipfile.ZIP_DEFLATED)
    archive.seek(0)
    return archive, count
