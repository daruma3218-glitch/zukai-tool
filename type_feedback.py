#!/usr/bin/env python3
"""ディレクターが種類を直した例を、次の回の判定に活かす（2026-10-14）。

「地図をやめて作り直す」などで種類を変えた文を output 直下の type_feedback.jsonl に残し、
次のジョブの抽出（extractor）に「過去にディレクターが直した例」として渡す。
素材スタジオの「ルート違い」（route_feedback.jsonl → ルーターの few-shot）と同じ考え方。
"""

import json
import threading
from datetime import datetime
from pathlib import Path

FILE_NAME = "type_feedback.jsonl"
MAX_SENTENCE = 120
_LOCK = threading.Lock()


def _clean(text: str) -> str:
    # 改行やかぎ括弧で、依頼文の箇条書きが崩れないようにする
    return " ".join(str(text or "").split()).replace("「", "『").replace("」", "』")[:MAX_SENTENCE]


def record(output_root: Path, excerpt: str, given_type: str, correct_type: str, job_id: str = "") -> None:
    sentence = _clean(excerpt)
    if not sentence or not given_type or given_type == correct_type:
        return
    line = json.dumps({"sentence": sentence, "given_type": given_type, "correct_type": correct_type,
                       "job_id": job_id, "at": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False)
    with _LOCK:
        with open(Path(output_root) / FILE_NAME, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def examples(output_root: Path, limit: int = 12) -> list:
    """新しい順に、同じ文は最後の判断だけを返す。読めない行は飛ばす。"""
    path = Path(output_root) / FILE_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    picked, seen = [], set()
    for raw in reversed(lines):
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        sentence = _clean(item.get("sentence")) if isinstance(item, dict) else ""
        if not sentence or sentence in seen:
            continue
        seen.add(sentence)
        if item.get("given_type") and item.get("correct_type") and item["given_type"] != item["correct_type"]:
            picked.append({"sentence": sentence, "given_type": str(item["given_type"]),
                           "correct_type": str(item["correct_type"])})
        if len(picked) >= limit:
            break
    return picked


def prompt_block(items: list) -> str:
    lines = [f"- 「{i['sentence']}」は {i['given_type']} ではなく {i['correct_type']} が合っていた"
             for i in items or [] if i.get("sentence")]
    if not lines:
        return ""
    return ("\n【過去にディレクターが種類を直した例（同じ判断の誤りを避けること）】\n" + "\n".join(lines) + "\n")
