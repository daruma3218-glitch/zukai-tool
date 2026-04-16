#!/usr/bin/env python3
"""メインパイプライン: 3 フェーズを順次実行

Phase 1: 原稿分析 + 視覚化ポイント抽出（Claude）
Phase 2: 抜粋 → 英文プロンプト（Claude、並列バッチ）
Phase 3: 英文プロンプト → 画像（Gemini、asyncio 並列）
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
from generator import run_parallel_generation, DEFAULT_CONCURRENCY


class DiagramPipeline:
    """図解生成パイプライン"""

    def __init__(
        self,
        manuscript_text: str,
        output_dir: Path,
        target_count: int = 50,
        user_instructions: str = "",
        concurrency: int = DEFAULT_CONCURRENCY,
        progress_callback: Optional[Callable] = None,
        log_callback: Optional[Callable] = None,
        item_callback: Optional[Callable] = None,
    ):
        self.manuscript_text = manuscript_text
        self.output_dir = Path(output_dir)
        self.target_count = max(1, min(target_count, 200))
        self.user_instructions = user_instructions
        self.concurrency = concurrency
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
        if not gemini_key:
            raise RuntimeError("GEMINI_API_KEY が設定されていません。")

        # Phase 0: バリデーション + 原稿保存
        self._progress(0, "原稿を保存中...", 1)
        manuscript_path = self.output_dir / "manuscript.txt"
        manuscript_path.write_text(self.manuscript_text, encoding="utf-8")
        self._log("setup", f"原稿を保存しました（{len(self.manuscript_text)}文字）")

        # Phase 1: 原稿分析
        self._progress(1, "原稿を分析中...", 3)
        self._log("analyze", "Claude で原稿の全体構造を分析しています...")
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
        self._log("prompter", "Claude で英文プロンプトを並列生成しています...")
        prompts = generate_all_prompts(
            client,
            excerpts,
            title=title,
            user_instructions=self.user_instructions,
            max_workers=5,
            log=self._log,
        )
        save_json(self.output_dir / "prompts.json", {"items": prompts})
        self._log("prompter", f"プロンプト生成完了: {len(prompts)} 件")
        self._progress(2, f"プロンプト生成完了: {len(prompts)} 件", 45)

        # Phase 3: 並列画像生成
        self._progress(3, f"画像を並列生成中（同時 {self.concurrency} 枚）...", 50)
        self._log(
            "generator",
            f"Gemini で画像 {len(prompts)} 枚を並列生成します",
            f"並列度: {self.concurrency} / モデル: gemini-3.1-flash-image-preview",
        )

        results = run_parallel_generation(
            api_key=gemini_key,
            prompts=prompts,
            output_dir=self.images_dir,
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

        # マニフェスト保存
        manifest = {
            "title": title,
            "summary": analysis.get("summary", ""),
            "keywords": keywords,
            "sections": sections,
            "user_instructions": self.user_instructions,
            "target_count": self.target_count,
            "concurrency": self.concurrency,
            "succeeded": success_count,
            "failed": fail_count,
            "items": results,
            "completed_at": datetime.now().isoformat(),
        }
        save_json(self.output_dir / "manifest.json", manifest)

        self._progress(4, f"完了: {success_count}/{len(prompts)} 枚生成", 100)
        return manifest
