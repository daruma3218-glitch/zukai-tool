#!/usr/bin/env python3
"""メインパイプライン: 3 フェーズ（+ 任意の内容検査）を順次実行

Phase 1: 原稿分析 + 視覚化ポイント抽出（ASTRA CLI）
Phase 2: 抜粋 → 英文プロンプト（ASTRA CLI、並列バッチ）
Phase 3: 英文プロンプト → 画像（既存画像モデル、asyncio 並列）
Phase 4: 完成画像と対応する抜粋の内容検査（ASTRA CLI）
         ※ content_review=True のときだけ実行（既定はオフ。時間がかかるため任意）
"""

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from utils import (
    get_anthropic_client,
    save_json,
    load_json,
)
from extractor import analyze_manuscript, extract_visual_points
from prompter import generate_all_prompts
from verifier import verify_images
from generator import (
    run_parallel_generation,
    DEFAULT_CONCURRENCY,
    PROVIDER_NANOBANANA,
    PROVIDER_GPT_IMAGE,
    VALID_PROVIDERS,
)


class DiagramPipeline:
    """図解生成パイプライン"""

    def __init__(
        self,
        manuscript_text: str,
        output_dir: Path,
        target_count: int = 50,
        user_instructions: str = "",
        worldview_preset: str = "",
        no_text_mode: bool = False,
        concurrency: int = DEFAULT_CONCURRENCY,
        provider: str = PROVIDER_NANOBANANA,
        openai_quality: str = "medium",
        content_review: bool = False,
        progress_callback: Optional[Callable] = None,
        log_callback: Optional[Callable] = None,
        item_callback: Optional[Callable] = None,
    ):
        self.manuscript_text = manuscript_text
        self.output_dir = Path(output_dir)
        self.target_count = max(1, min(target_count, 200))
        self.user_instructions = user_instructions
        self.worldview_preset = (worldview_preset or "").strip()
        self.no_text_mode = bool(no_text_mode)
        self.concurrency = concurrency
        self.provider = provider if provider in VALID_PROVIDERS else PROVIDER_NANOBANANA
        self.openai_quality = openai_quality
        self.content_review = bool(content_review)
        self.progress_callback = progress_callback or (lambda phase, msg, pct: None)
        self.log_callback = log_callback or (lambda *a, **kw: None)
        self.item_callback = item_callback or (lambda info: None)

        # ジョブ用ディレクトリ
        self.images_dir = self.output_dir / "images"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)

        # 進捗状態を共有
        self._items: dict[int, dict] = {}
        self._items_lock = threading.Lock()

    # ---- 内部ヘルパ ----
    def _log(self, category: str, message: str, detail: str = ""):
        print(f"  [{category}] {message}" + (f" - {detail}" if detail else ""), flush=True)
        try:
            self.log_callback(category, message, detail)
        except Exception:
            pass

    def _progress(self, phase: int, message: str, percent: int):
        print(f"  [Phase {phase}] {message} ({percent}%)", flush=True)
        try:
            self.progress_callback(phase, message, percent)
        except Exception:
            pass

    def _on_item_event(self, info: dict):
        """画像 1 枚の進捗イベント"""
        idx = info.get("index", 0)
        with self._items_lock:
            existing = self._items.get(idx, {})
            existing.update(info)
            self._items[idx] = existing

        # ファイルにも進捗スナップショットを保存
        self._dump_progress_snapshot()

        try:
            self.item_callback(info)
        except Exception:
            pass

    def _dump_progress_snapshot(self):
        """images_progress.json に最新スナップショットを書き出す"""
        with self._items_lock:
            items = list(self._items.values())
        items.sort(key=lambda x: x.get("index", 0))
        snapshot = {
            "items": items,
            "updated_at": datetime.now().isoformat(),
        }
        try:
            (self.output_dir / "images_progress.json").write_text(
                json.dumps(snapshot, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ---- メインフロー ----
    def run(self) -> dict:
        client = get_anthropic_client()
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        openai_key = os.environ.get("OPENAI_API_KEY", "")

        # プロバイダ別の API キー必須チェック
        if self.provider == PROVIDER_NANOBANANA and not gemini_key:
            raise RuntimeError("nanobanana を使うには GEMINI_API_KEY が必要です。")
        if self.provider == PROVIDER_GPT_IMAGE and not openai_key:
            raise RuntimeError("gpt-image を使うには OPENAI_API_KEY が必要です。")

        # Phase 0: バリデーション + 原稿保存
        self._progress(0, "原稿を保存中...", 1)
        manuscript_path = self.output_dir / "manuscript.txt"
        manuscript_path.write_text(self.manuscript_text, encoding="utf-8")
        self._log("setup", f"原稿を保存しました（{len(self.manuscript_text)}文字）")

        # Phase 1: 原稿分析
        self._progress(1, "原稿を分析中...", 3)
        self._log("analyze", "ASTRAで原稿の全体構造を分析しています...")
        analysis = analyze_manuscript(client, self.manuscript_text, log=self._log)
        title = analysis.get("title", "無題")
        sections = analysis.get("sections", [])
        keywords = analysis.get("keywords", [])
        self._log(
            "analyze",
            f"分析完了: 「{title}」",
            f"キーワード {len(keywords)}個 / セクション {len(sections)}個",
        )
        save_json(self.output_dir / "analysis.json", analysis)
        self._progress(1, f"分析完了: {title}", 8)

        # Phase 1b: 視覚化ポイント抽出
        self._progress(1, f"視覚化ポイントを {self.target_count} 個抽出中...", 12)
        self._log(
            "extract",
            f"原稿から {self.target_count} 個の視覚化ポイントを抽出中...",
            f"目標: {self.target_count} 個 / ユーザー指示: {'あり' if self.user_instructions else 'なし'}",
        )
        excerpts = extract_visual_points(
            client,
            self.manuscript_text,
            analysis,
            target_count=self.target_count,
            user_instructions=self.user_instructions,
            log=self._log,
        )
        save_json(self.output_dir / "excerpts.json", {"items": excerpts})
        self._log("extract", f"抽出完了: {len(excerpts)} 個")
        self._progress(1, f"抽出完了: {len(excerpts)} 個の視覚化ポイント", 25)

        # 抜粋情報をフロントの初期表示に使えるよう、items を初期化
        with self._items_lock:
            for ex in excerpts:
                idx = ex.get("index", 0)
                self._items[idx] = {
                    "index": idx,
                    "status": "pending",
                    "section": ex.get("section", ""),
                    "excerpt": ex.get("excerpt", ""),
                    "keypoint": ex.get("keypoint", ""),
                    "type": ex.get("type", ""),
                }
        self._dump_progress_snapshot()

        # Phase 2: 英文プロンプト生成（並列バッチ）
        self._progress(2, "英文プロンプトを並列生成中...", 30)
        self._log("prompter", "ASTRAで図の内容と画像指示を設計しています...")
        prompts = generate_all_prompts(
            client,
            excerpts,
            title=title,
            user_instructions=self.user_instructions,
            worldview_preset=self.worldview_preset,
            no_text_mode=self.no_text_mode,
            max_workers=5,
            log=self._log,
        )
        save_json(self.output_dir / "prompts.json", {"items": prompts})
        self._log("prompter", f"プロンプト生成完了: {len(prompts)} 件")
        self._progress(2, f"プロンプト生成完了: {len(prompts)} 件", 45)

        # Phase 3: 並列画像生成
        provider_label = "nanobanana (Gemini)" if self.provider == PROVIDER_NANOBANANA else "gpt-image (OpenAI)"
        self._progress(3, f"画像を並列生成中（{provider_label} / 同時 {self.concurrency} 枚）...", 50)
        self._log(
            "generator",
            f"{provider_label} で画像 {len(prompts)} 枚を並列生成します",
            f"並列度: {self.concurrency}",
        )

        results = run_parallel_generation(
            prompts=prompts,
            output_dir=self.images_dir,
            provider=self.provider,
            gemini_api_key=gemini_key,
            openai_api_key=openai_key,
            openai_quality=self.openai_quality,
            concurrency=self.concurrency,
            progress_callback=self._on_item_event,
        )

        # 結果サマリ
        success_count = sum(1 for r in results if r.get("success"))
        fail_count = len(results) - success_count
        self._log(
            "generator",
            f"画像生成完了: 成功 {success_count} 枚 / 失敗 {fail_count} 枚",
        )

        # Phase 4（任意）: 完成画像と原稿抜粋の内容検査
        # 既定はオフ。オンでも検査の失敗で完了・ZIP ダウンロードを止めない。
        content_review = None
        if self.content_review:
            content_review = self._run_content_review(results)

        # マニフェスト保存
        manifest = {
            "title": title,
            "summary": analysis.get("summary", ""),
            "keywords": keywords,
            "sections": sections,
            "user_instructions": self.user_instructions,
            "worldview_preset": self.worldview_preset,
            "no_text_mode": self.no_text_mode,
            "target_count": self.target_count,
            "concurrency": self.concurrency,
            "provider": self.provider,
            "openai_quality": self.openai_quality if self.provider == PROVIDER_GPT_IMAGE else None,
            "content_review_enabled": self.content_review,
            "succeeded": success_count,
            "failed": fail_count,
            "items": results,
            "content_review": content_review,
            "completed_at": datetime.now().isoformat(),
        }
        save_json(self.output_dir / "manifest.json", manifest)

        done_msg = f"完了: {success_count}/{len(prompts)}枚生成"
        if content_review:
            review_counts = content_review["counts"]
            done_msg += f"・内容の要修正 {review_counts['needs_fix']}枚／未確認 {review_counts['unverified']}枚"
            self._progress(4, done_msg, 100)
        else:
            self._progress(3, done_msg, 100)
        return manifest

    def _run_content_review(self, results: list) -> Optional[dict]:
        """Phase 4: 内容検査。失敗しても None を返すだけで、パイプライン全体は完了させる。"""
        self._progress(4, "完成画像と原稿の抜粋を照合中...", 90)

        def on_review(item, review):
            item["content_review"] = review
            self._on_item_event({**item, "status": "ok"})

        try:
            content_review = verify_images(
                results, self.images_dir, job_id=self.output_dir.name, on_review=on_review,
            )
        except Exception as e:  # 検査は補助機能。画像は揃っているので完了を優先する
            self._log("review", "内容検査を中断しました（画像は生成済みのためそのまま完了します）", str(e)[:300])
            return None
        save_json(self.output_dir / "content_review.json", content_review)
        review_counts = content_review["counts"]
        self._log(
            "review",
            f"内容検査: 合格 {review_counts['pass']} / 要修正 {review_counts['needs_fix']} / 未確認 {review_counts['unverified']}",
        )
        return content_review
