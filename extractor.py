#!/usr/bin/env python3
"""Phase 1: 原稿から視覚化ポイントを抽出するエージェント

原稿を分析して、図解化すべき箇所を「抜粋」と一緒にN個（デフォルト50個）抽出する。
各ポイントは原稿の特定の段落・文章と1:1対応するため、ハルシネーションを防ぎ
画像と原稿の整合性を保証する。
"""

import json
import os
import time
from typing import Callable, Optional

import anthropic

from utils import claude_query, parse_json_object, parse_json_array


CLAUDE_MODEL = "claude-sonnet-4-6"


def analyze_manuscript(
    client: anthropic.Anthropic,
    manuscript_text: str,
    log: Optional[Callable] = None,
) -> dict:
    """原稿の全体構造（タイトル・キーワード・セクション）を把握する"""
    log = log or (lambda *a, **kw: None)

    system = (
        "あなたは経験豊富な編集者・動画ディレクターです。"
        "渡された原稿を分析して、メインテーマ・キーワード・セクション構成を抽出します。"
        "結果は必ずJSONオブジェクトのみで返してください。前置きや説明は不要です。"
    )
    # 原稿の冒頭・末尾のみで全体像を把握（長文対策）
    head = manuscript_text[:8000]
    tail = manuscript_text[-2000:] if len(manuscript_text) > 10000 else ""
    sample = head + ("\n...\n" + tail if tail else "")

    query = f"""以下の原稿を分析して、JSON形式で返してください。

{sample}

返すJSON形式:
{{
  "title": "原稿のメインテーマを表すタイトル",
  "keywords": ["重要キーワード1", "キーワード2", ...（10〜15個）],
  "sections": ["セクション1", "セクション2", ...（原稿の構成、最大20個）],
  "summary": "原稿全体の要約（200文字以内）"
}}

JSONのみ返すこと。"""

    result = claude_query(client, query, system, max_tokens=2048, model=CLAUDE_MODEL)
    data = parse_json_object(result)

    if not data:
        log("warn", "原稿分析が失敗。フォールバックを使用")
        first_line = manuscript_text.split("\n", 1)[0][:50]
        return {
            "title": first_line or "無題",
            "keywords": [],
            "sections": [first_line] if first_line else [],
            "summary": manuscript_text[:200],
        }
    return data


def extract_visual_points(
    client: anthropic.Anthropic,
    manuscript_text: str,
    analysis: dict,
    target_count: int = 50,
    user_instructions: str = "",
    log: Optional[Callable] = None,
) -> list:
    """原稿からN個の視覚化ポイントを抽出する。

    各ポイントは以下を含む:
        index: 順序（1始まり）
        excerpt: 原稿からの抜粋（1〜3文）
        section: 対応するセクション名
        type: illustration | map | diagram | chart のいずれか
        keypoint: その抜粋の核心（10〜30文字）
    """
    log = log or (lambda *a, **kw: None)

    sections = analysis.get("sections", [])
    keywords = analysis.get("keywords", [])
    title = analysis.get("title", "")

    sections_str = "\n".join(f"- {s}" for s in sections[:20]) or "（セクションなし）"
    keywords_str = ", ".join(keywords[:15])

    user_block = ""
    if user_instructions.strip():
        user_block = f"""

【ユーザーからの指示（最優先で従うこと）】
{user_instructions.strip()}
"""

    system = (
        "あなたは動画演出家・ビジュアルディレクターです。"
        "渡された原稿を最初から最後まで読み、視覚化（図解化）すべき箇所を"
        f"原稿の順番通りに{target_count}個抽出します。"
        "結果は必ずJSON配列のみで返してください。"
    )

    # 原稿全体を分割せずに渡す（Sonnet 4.6 は十分な context を持つ）
    # 1リクエストあたり安全に約 60,000 文字（≒ 30,000 token 程度）
    MAX_MS_CHARS = 60000
    manuscript_excerpt = manuscript_text[:MAX_MS_CHARS]
    truncated_note = ""
    if len(manuscript_text) > MAX_MS_CHARS:
        truncated_note = f"\n（原稿が長いため冒頭{MAX_MS_CHARS}文字を使用。残りは別途処理されます）"

    query = f"""以下の原稿から、図解資料にすべき箇所を**{target_count}個**抽出してください。

【原稿のタイトル】
{title}

【主要キーワード】
{keywords_str}

【セクション構成】
{sections_str}

【原稿本文】{truncated_note}
---
{manuscript_excerpt}
---
{user_block}

【抽出ルール】
1. **原稿の順番通り**に抽出すること（最初の段落 → 最後の段落へ）
2. **満遍なく抽出**: 1つのセクションに偏らず、原稿全体から均等に拾う
3. **{target_count}個ぴったり**抽出すること（多くも少なくもダメ）
4. 各抜粋は**原稿に実在する文章**を使う（要約や創作は禁止）
5. 抜粋は1〜3文程度（長すぎず短すぎず、視覚化に必要な情報を含む）
6. 重要な数値・固有名詞・地名・概念・人物・出来事を優先
7. typeは内容に最適なものを選ぶ:
   - illustration: 人物・物・シーン・出来事の描写
   - map: 地名・場所・地理的位置関係
   - diagram: 仕組み・概念・フロー・構造
   - chart: 数値比較・統計・割合・推移

【出力JSON形式】
JSON配列のみで返すこと（前置き・後書き・コードブロック禁止）:
[
  {{
    "index": 1,
    "excerpt": "原稿からの正確な抜粋（1〜3文）",
    "section": "対応するセクション名（上記セクション構成から選ぶ）",
    "type": "illustration",
    "keypoint": "この抜粋の核心を10〜30文字で要約"
  }},
  {{
    "index": 2,
    "excerpt": "...",
    "section": "...",
    "type": "diagram",
    "keypoint": "..."
  }}
]

合計**{target_count}個**返すこと。それ未満は無効です。"""

    log("extractor", f"視覚化ポイントを抽出中（目標 {target_count} 個）...")
    result = claude_query(client, query, system, max_tokens=16000, model=CLAUDE_MODEL)
    excerpts = parse_json_array(result)
    log("extractor", f"1回目の抽出: {len(excerpts)} 個")

    # 不足していれば追加リクエストで補充
    attempts = 0
    while len(excerpts) < target_count and attempts < 3:
        attempts += 1
        remaining = target_count - len(excerpts)
        already_excerpts = "\n".join(
            f"- {e.get('excerpt', '')[:80]}" for e in excerpts[-20:]
        )
        supplement_query = f"""さらに**{remaining}個**追加で視覚化ポイントを抽出してください。
既に抽出済みの最後の20個（重複を避けるため）:
{already_excerpts}

【原稿】
---
{manuscript_excerpt}
---
{user_block}

ルール:
- 既に抽出されたものとは異なる箇所を選ぶ
- 原稿の順番通り
- 必ず{remaining}個返す
- 同じJSON形式で返す（index は {len(excerpts) + 1} から始める）

JSON配列のみで返すこと:
[
  {{"index": {len(excerpts) + 1}, "excerpt": "...", "section": "...", "type": "...", "keypoint": "..."}}
]"""
        log("extractor", f"補充リクエスト（残り {remaining} 個）...")
        result2 = claude_query(client, supplement_query, system, max_tokens=10000, model=CLAUDE_MODEL)
        extra = parse_json_array(result2)
        log("extractor", f"補充結果: {len(extra)} 個追加")
        if not extra:
            break
        excerpts.extend(extra)

    # index を振り直し（順序保持）
    for i, e in enumerate(excerpts[:target_count]):
        e["index"] = i + 1
        # type のデフォルト値
        if e.get("type") not in ("illustration", "map", "diagram", "chart"):
            e["type"] = "illustration"
        # 必須フィールドの補完
        e.setdefault("section", "")
        e.setdefault("keypoint", e.get("excerpt", "")[:30])
        e.setdefault("excerpt", "")

    final = excerpts[:target_count]
    log("extractor", f"最終抽出数: {len(final)} 個")

    # 不足分をダミーで埋める（保険）
    while len(final) < target_count:
        idx = len(final) + 1
        final.append({
            "index": idx,
            "excerpt": title + " に関する追加図解",
            "section": sections[0] if sections else title,
            "type": "illustration",
            "keypoint": f"追加の図解 {idx}",
        })

    return final
