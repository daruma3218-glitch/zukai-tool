#!/usr/bin/env python3
"""Phase 2: 視覚化ポイント → 英文画像プロンプト

抽出した視覚化ポイント（excerpt + type + section）を画像生成モデル向けの
英文プロンプトに変換する。並列バッチ処理で高速化する。
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import anthropic

from utils import claude_query, parse_json_array


CLAUDE_MODEL = "claude-sonnet-4-6"
BATCH_SIZE = 10  # 1 リクエストあたりのプロンプト数（並列バッチ）


def _build_user_block(user_instructions: str) -> str:
    if not user_instructions.strip():
        return ""
    return f"""
【ユーザーからの画像指示（最優先で従うこと）】
{user_instructions.strip()}
"""


def generate_prompts_batch(
    client: anthropic.Anthropic,
    excerpts_batch: list,
    title: str,
    user_instructions: str = "",
) -> list:
    """1 バッチ（10 件程度）の視覚化ポイントを英文プロンプト化"""
    user_block = _build_user_block(user_instructions)
    excerpts_json = json.dumps(excerpts_batch, ensure_ascii=False, indent=2)

    system = (
        "You are a visual director who converts Japanese manuscript excerpts "
        "into precise English image prompts for an image generation AI. "
        "Each prompt MUST faithfully represent its source excerpt. "
        "Return only a JSON array. No markdown, no commentary."
    )

    query = f"""以下は動画原稿「{title}」から抽出した{len(excerpts_batch)}個の視覚化ポイントです。
各項目に**厳密に対応する**英文画像プロンプトを作成してください。

視覚化ポイント:
{excerpts_json}
{user_block}

【必須ルール】
1. プロンプトは英語で記述（画像生成モデル向け）
2. 画像内の文字は**日本語のみ**にすること（"text in Japanese only" 等を明記）
3. 画像にタイトル文字は不要（"no title text", "no heading" を明記）
4. **16:9 横長**（"16:9 aspect ratio, landscape orientation"）
5. **シンプルでわかりやすい**仕上がり（情報過多にしない）
6. 内容に応じてイラストのタッチを変える:
   - illustration: 水彩風 / フラット / 線画 / 切り絵 / 3D風 / コミック風 / ミニマルから最適なものを選ぶ
   - map: 航空写真風（aerial / satellite imagery style）。日本語の地名ラベル
   - diagram: 概念図・フロー図（矢印とボックス、3〜5要素まで）
   - chart: 棒グラフ・円グラフ・推移グラフ（要素は3〜5個まで、数値は具体的に）
7. カラフル可（パステル・ビビッド・モノトーンなど自由）
8. 元の excerpt の情報（数値・固有名詞・地名）をプロンプトに反映する
9. 各プロンプトは互いに**異なるビジュアル**にする（同じ構図の連発禁止）

【出力JSON形式】
JSON配列のみで返すこと（マークダウン禁止）:
[
  {{
    "index": (元のindexをそのまま使う),
    "prompt": "英語プロンプト（スタイル指定・日本語ラベル指定・no title text を含む）",
    "section": "セクション名",
    "excerpt": "元の抜粋（そのまま）",
    "type": "元のtype（そのまま）",
    "keypoint": "元のkeypoint（そのまま）"
  }}
]

必ず{len(excerpts_batch)}個出力すること（順序は入力と同じ）。"""

    result = claude_query(client, query, system, max_tokens=8000, model=CLAUDE_MODEL)
    prompts = parse_json_array(result)

    # 入力の excerpt 情報をマージ（プロンプト生成側で抜けても保持）
    prompts_by_index = {p.get("index"): p for p in prompts if p.get("prompt")}
    merged = []
    for ex in excerpts_batch:
        idx = ex.get("index")
        if idx in prompts_by_index:
            p = prompts_by_index[idx]
            # 入力フィールドで補完
            p.setdefault("section", ex.get("section", ""))
            p.setdefault("excerpt", ex.get("excerpt", ""))
            p.setdefault("type", ex.get("type", "illustration"))
            p.setdefault("keypoint", ex.get("keypoint", ""))
            merged.append(p)
        else:
            # フォールバック: 簡易プロンプトを生成
            ex_text = ex.get("excerpt", "")[:100]
            t = ex.get("type", "illustration")
            fallback_prompt = (
                f"A {t} representing: {ex_text}. "
                "Simple, clear visual style. Japanese text labels only. "
                "No title text. 16:9 landscape orientation."
            )
            merged.append({
                "index": idx,
                "prompt": fallback_prompt,
                "section": ex.get("section", ""),
                "excerpt": ex.get("excerpt", ""),
                "type": t,
                "keypoint": ex.get("keypoint", ""),
            })
    return merged


def generate_all_prompts(
    client: anthropic.Anthropic,
    excerpts: list,
    title: str,
    user_instructions: str = "",
    max_workers: int = 5,
    log: Optional[Callable] = None,
) -> list:
    """全視覚化ポイントを並列バッチで英文プロンプト化"""
    log = log or (lambda *a, **kw: None)

    # 10 件ずつバッチに分割
    batches = []
    for i in range(0, len(excerpts), BATCH_SIZE):
        batches.append(excerpts[i:i + BATCH_SIZE])

    log("prompter", f"{len(excerpts)} 件を {len(batches)} バッチに分割（同時 {max_workers} 並列）")

    all_results = [None] * len(excerpts)
    completed_batches = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_batch = {
            executor.submit(generate_prompts_batch, client, batch, title, user_instructions): idx
            for idx, batch in enumerate(batches)
        }
        for future in as_completed(future_to_batch):
            batch_idx = future_to_batch[future]
            try:
                results = future.result()
                # 元の順序に戻す
                for r in results:
                    orig_idx = r.get("index", 0) - 1
                    if 0 <= orig_idx < len(all_results):
                        all_results[orig_idx] = r
                completed_batches += 1
                log("prompter", f"バッチ {completed_batches}/{len(batches)} 完了（{len(results)} 件）")
            except Exception as e:
                log("error", f"バッチ {batch_idx} 失敗: {str(e)[:100]}")

    # None を埋める（フォールバック）
    final = []
    for i, r in enumerate(all_results):
        if r is None:
            ex = excerpts[i] if i < len(excerpts) else {}
            ex_text = ex.get("excerpt", "")[:100]
            t = ex.get("type", "illustration")
            final.append({
                "index": i + 1,
                "prompt": (
                    f"A {t} representing: {ex_text}. "
                    "Simple visual. Japanese text only. No title. 16:9."
                ),
                "section": ex.get("section", ""),
                "excerpt": ex.get("excerpt", ""),
                "type": t,
                "keypoint": ex.get("keypoint", ""),
            })
        else:
            final.append(r)

    return final
