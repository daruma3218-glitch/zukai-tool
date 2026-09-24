#!/usr/bin/env python3
"""メインパイプライン: 3 フェーズを順次実行

Phase 1: 原稿分析 + 視覚化ポイント抽出（ASTRA CLI）
Phase 2: 抜粋 → 英文プロンプト（ASTRA CLI、並列バッチ）
Phase 3: 英文プロンプト → 画像（既存画像モデル、asyncio 並列）
"""

import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from utils import (
    get_anthropic_client,
    save_json,
)
from extractor import analyze_manuscript, extract_visual_points
from prompter import generate_all_prompts, CANDIDATE_MODES, MAP_MODES
import candidates
import map_renderer
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
        openai_model: Optional[str] = None,
        progress_callback: Optional[Callable] = None,
        log_callback: Optional[Callable] = None,
        item_callback: Optional[Callable] = None,
        candidate_mode: str = "single",
        map_mode: str = "ai",
    ):
        self.candidate_mode = candidate_mode if candidate_mode in CANDIDATE_MODES else "single"
        self.map_mode = map_mode if map_mode in MAP_MODES else "ai"
        self.manuscript_text = manuscript_text
        self.output_dir = Path(output_dir)
        self.target_count = max(1, min(target_count, 200))
        self.user_instructions = user_instructions
        self.worldview_preset = (worldview_preset or "").strip()
        self.no_text_mode = bool(no_text_mode)
        self.concurrency = concurrency
        self.provider = provider if provider in VALID_PROVIDERS else PROVIDER_NANOBANANA
        self.openai_quality = openai_quality
        self.openai_model = (openai_model or "").strip() or None
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

        if info.get("status") in ("ok", "failed"):
            with self._items_lock:
                total = len(self._items)
                finished = sum(item.get("status") in ("ok", "failed") for item in self._items.values())
            self._progress(3, f"画像を生成中: {finished}/{total}枚処理済み", 50 + int(49 * finished / max(total, 1)))

        try:
            self.item_callback(info)
        except Exception:
            pass

    def _dump_progress_snapshot(self):
        """images_progress.json に最新スナップショットを書き出す"""
        with self._items_lock:
            snapshot = {
                "items": sorted(self._items.values(), key=lambda x: x.get("index", 0)),
                "updated_at": datetime.now().isoformat(),
            }
            try:
                save_json(self.output_dir / "images_progress.json", snapshot)
            except OSError as exc:
                self._log("warn", "画像の進捗を保存できませんでした", str(exc))

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
            candidate_mode=self.candidate_mode,
            map_mode=self.map_mode,
        )
        self._log("prompter", f"プロンプト生成完了: {len(prompts)} 件")

        # 候補（複数案）と地図を、1枚ずつの生成項目に展開する。1案なら従来と同じ番号・ファイル名。
        groups = prompts
        prompts = candidates.expand(groups, self.candidate_mode, self.map_mode,
                                    map_enabled=map_renderer.enabled(), no_text=self.no_text_mode)
        plan = candidates.plan_counts(prompts)
        if self.candidate_mode != "single" or plan["maps"]:
            save_json(self.output_dir / "prompt_groups.json", {"items": groups})
            self._log("prompter", f"{plan['groups']}箇所 → 画像 {plan['images']}枚"
                      f"（画像AI {plan['ai_images']}枚・地図データ {plan['maps']}枚）")
        save_json(self.output_dir / "prompts.json", {"items": prompts})
        with self._items_lock:
            self._items = {p["index"]: {"index": p["index"], "status": "pending",
                                        **{k: p.get(k, "") for k in ("section", "excerpt", "keypoint", "type",
                                                                     "group", "variant", "variant_label")}}
                           for p in prompts}
        self._dump_progress_snapshot()
        self._progress(2, f"プロンプト生成完了: {len(prompts)} 枚分", 45)

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
            openai_model=self.openai_model,
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
            "worldview_preset": self.worldview_preset,
            "no_text_mode": self.no_text_mode,
            "target_count": self.target_count,
            "concurrency": self.concurrency,
            "provider": self.provider,
            "openai_quality": self.openai_quality if self.provider == PROVIDER_GPT_IMAGE else None,
            "openai_model": self.openai_model,
            "candidate_mode": self.candidate_mode,
            "map_mode": self.map_mode,
            "groups": plan["groups"],
            "images_planned": plan["images"],
            "ai_images": plan["ai_images"],
            "maps": plan["maps"],
            "succeeded": success_count,
            "failed": fail_count,
            "items": results,
            "completed_at": datetime.now().isoformat(),
        }
        save_json(self.output_dir / "manifest.json", manifest)

        self._progress(3, f"完了: {success_count}/{len(prompts)}枚生成（失敗 {fail_count}枚）", 100)
        return manifest
