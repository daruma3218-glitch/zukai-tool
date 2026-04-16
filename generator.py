#!/usr/bin/env python3
"""Phase 3: 並列画像生成エージェント

asyncio + Semaphore で同時 N 枚を並列生成する。Gemini SDK は同期なので
loop.run_in_executor で thread pool に委譲する（真の並列化）。
"""

import asyncio
import base64
import os
import time
from pathlib import Path
from typing import Callable, Optional

from google import genai
from google.genai import types


IMAGE_MODEL = "gemini-3.1-flash-image-preview"
DEFAULT_CONCURRENCY = 12  # 同時並列数
MAX_RETRIES = 3


def _build_full_prompt(user_prompt: str, prompt_type: str = "illustration") -> str:
    """画像生成用のシステム接頭辞を付与"""
    style_hints = {
        "illustration": (
            "Style: choose the most fitting illustration style for the content "
            "(watercolor, flat, line art, paper-cut, 3D rendered, comic, or minimal). "
        ),
        "map": (
            "Style: aerial photograph / satellite imagery style map. "
            "Show geographical features clearly with Japanese place name labels. "
        ),
        "diagram": (
            "Style: clean conceptual diagram with arrows and 3-5 boxes. "
            "Minimal lines, clear structure, easy to understand at a glance. "
        ),
        "chart": (
            "Style: clean chart (bar / pie / line graph) with 3-5 data elements. "
            "Bold numbers, clear labels in Japanese. "
        ),
    }
    style = style_hints.get(prompt_type, style_hints["illustration"])

    common = (
        "IMPORTANT REQUIREMENTS: "
        "- All text, labels, numbers in the image MUST be in Japanese (日本語) only. "
        "- Do NOT include any English text in the image. "
        "- Do NOT add any title text or heading at the top of the image. "
        "- 16:9 landscape aspect ratio for video presentation. "
        "- Simple, clear, professional. Avoid clutter. "
    )
    return f"{style}{common}\n\nContent to visualize:\n{user_prompt}"


def _sync_generate_image(
    client: genai.Client,
    full_prompt: str,
    output_path: Path,
) -> tuple[bool, str]:
    """1 枚の画像を同期生成（thread pool で呼ばれる）

    戻り値: (success, error_message)
    """
    last_error = ""
    for attempt in range(MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=IMAGE_MODEL,
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"],
                ),
            )

            # response.parts → 画像データを探す
            parts_iter = []
            if hasattr(response, "parts") and response.parts:
                parts_iter = response.parts
            elif hasattr(response, "candidates") and response.candidates:
                cand = response.candidates[0]
                if cand.content and cand.content.parts:
                    parts_iter = cand.content.parts

            for part in parts_iter:
                inline = getattr(part, "inline_data", None)
                if inline and inline.mime_type and inline.mime_type.startswith("image/"):
                    img_data = inline.data
                    if isinstance(img_data, str):
                        img_data = base64.b64decode(img_data)
                    output_path.write_bytes(img_data)
                    return True, ""

            last_error = "no image in response"
            # テキスト返答があれば原因の手がかりに
            for part in parts_iter:
                txt = getattr(part, "text", None)
                if txt:
                    last_error = f"text only: {txt[:120]}"
                    break

        except Exception as e:
            err = str(e)
            last_error = err[:200]
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                # レート制限: 30 秒 x attempt+1 待機
                time.sleep(30 * (attempt + 1))
                continue
            if "safety" in err.lower() or "block" in err.lower():
                return False, f"safety blocked: {err[:120]}"
            if "not found" in err.lower() or "404" in err:
                return False, f"model not available: {IMAGE_MODEL}"
            time.sleep(3 + 2 * attempt)

    return False, last_error or "max retries exceeded"


class ParallelImageGenerator:
    """asyncio + Semaphore による並列画像生成器"""

    def __init__(
        self,
        api_key: str,
        concurrency: int = DEFAULT_CONCURRENCY,
        progress_callback: Optional[Callable[[dict], None]] = None,
    ):
        self.client = genai.Client(api_key=api_key)
        self.concurrency = max(1, min(concurrency, 32))
        self.progress_callback = progress_callback or (lambda info: None)
        # Lock は async 関数内で生成する（Python 3.9 では __init__ で
        # asyncio.Lock() を作ると get_event_loop() エラーになる）
        self._counter_lock: Optional[asyncio.Lock] = None
        self._completed = 0
        self._failed = 0
        self._total = 0

    async def _generate_one(
        self,
        prompt_entry: dict,
        output_dir: Path,
        semaphore: asyncio.Semaphore,
    ) -> dict:
        """1 枚を生成（semaphore で並列度制御）"""
        idx = prompt_entry.get("index", 0)
        prompt_text = prompt_entry.get("prompt", "")
        prompt_type = prompt_entry.get("type", "illustration")
        section = prompt_entry.get("section", "")
        excerpt = prompt_entry.get("excerpt", "")
        keypoint = prompt_entry.get("keypoint", "")
        filename = f"diagram_{idx:03d}.png"
        output_path = output_dir / filename

        async with semaphore:
            # 開始通知
            self.progress_callback({
                "index": idx,
                "status": "generating",
                "section": section,
                "keypoint": keypoint,
                "excerpt": excerpt,
            })

            full_prompt = _build_full_prompt(prompt_text, prompt_type)
            loop = asyncio.get_running_loop()

            try:
                success, error = await loop.run_in_executor(
                    None,
                    _sync_generate_image,
                    self.client,
                    full_prompt,
                    output_path,
                )
            except Exception as e:
                success, error = False, str(e)[:200]

            async with self._counter_lock:
                if success:
                    self._completed += 1
                else:
                    self._failed += 1
                completed_now = self._completed
                failed_now = self._failed

            result = {
                "index": idx,
                "filename": filename if success else None,
                "section": section,
                "excerpt": excerpt,
                "keypoint": keypoint,
                "type": prompt_type,
                "prompt": prompt_text,
                "success": success,
                "error": error if not success else "",
            }

            # 完了通知（リアルタイムでフロントへ）
            self.progress_callback({
                "index": idx,
                "status": "ok" if success else "failed",
                "section": section,
                "keypoint": keypoint,
                "excerpt": excerpt,
                "filename": filename if success else None,
                "error": error if not success else "",
                "completed_total": completed_now,
                "failed_total": failed_now,
                "grand_total": self._total,
            })

            return result

    async def generate_all(
        self,
        prompts: list,
        output_dir: Path,
    ) -> list:
        """全プロンプトを並列生成"""
        output_dir.mkdir(parents=True, exist_ok=True)
        self._completed = 0
        self._failed = 0
        self._total = len(prompts)

        # asyncio オブジェクトは running loop の中で生成する
        self._counter_lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(self.concurrency)
        tasks = [
            self._generate_one(p, output_dir, semaphore)
            for p in prompts
        ]
        # gather で並列実行（順序は保たれる）
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return results


def run_parallel_generation(
    api_key: str,
    prompts: list,
    output_dir: Path,
    concurrency: int = DEFAULT_CONCURRENCY,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> list:
    """同期エントリポイント: pipeline から呼び出す"""
    generator = ParallelImageGenerator(
        api_key=api_key,
        concurrency=concurrency,
        progress_callback=progress_callback,
    )
    return asyncio.run(generator.generate_all(prompts, output_dir))
