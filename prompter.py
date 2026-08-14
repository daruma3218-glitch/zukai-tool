#!/usr/bin/env python3
"""Phase 2: 視覚化ポイント → 英文画像プロンプト

抽出した視覚化ポイント（excerpt + type + section）を画像生成モデル向けの
英文プロンプトに変換する。並列バッチ処理で高速化する。
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

import anthropic

from utils import claude_query, parse_json_array


# プロンプト生成は品質最優先で Opus 5（センテンスつくーると同方針）。
# 環境変数 PROMPTER_MODEL で変更可（例: claude-sonnet-4-6 で従来に戻す）。
CLAUDE_MODEL = os.environ.get("PROMPTER_MODEL", "").strip() or "claude-opus-5"
BATCH_SIZE = 10  # 1 リクエストあたりのプロンプト数（並列バッチ）

# 世界観プリセット（①）: さゆみさんの手直し実績で最多だった
# 「人物が日本人風・背景が日本・¥表記」への対策。UIのプリセット選択で適用する。
WORLDVIEW_PRESETS = {
    "roshia": """
【ロシア解体新書 世界観（既定スタイル・全プロンプトに適用）】
※これは「画風・舞台のスタイル指定」であり情報の追加ではない。下の【必須ルール】9
（excerpt外の情報禁止）の**例外**として全プロンプトに適用する。
※ただし優先順位は **excerpt の明示 > この既定**。excerpt に国籍・舞台・通貨が
書かれている場合は必ず excerpt に従う（例: カダフィやリビアの側近はアラブ系の外見で
描く。「日本円にして◯◯円」とあればその金額だけ ¥ で描く）。
- 人物: excerpt から国籍・出身が読み取れる人物は**その民族的外見**で描く。
  読み取れない場合の既定はロシア/東欧系（"Russian / Eastern European (slavic) features" を明記）。
  無条件に日本人風の顔立ち・日本の学生服・日本のサラリーマンにするのは禁止
- 背景・街並み・室内: excerpt に舞台（国・都市）が明示されていれば**その土地の景観**。
  明示が無い場合の既定はロシア・旧ソ連圏（"Russian setting", "Soviet-era architecture",
  "Moscow cityscape" 等を明記）。無条件に日本の街並み・東京にするのは禁止
- 通貨・金額表現: excerpt に通貨が明示されていれば**その通貨の記号**を使う
  （ルーブル=₽、円=¥、ドル=$）。**1枚に複数の通貨が出る場合は、それぞれの金額に
  正しい記号を対応させる**（ルーブルと円の換算なら ₽ と ¥ を並べる。両方 ¥ や
  両方 ₽ にしない）。通貨の明示が無い金額の既定はルーブル ₽（その場合 ¥ は禁止）
【タイプ別の適用範囲（重要）】
- illustration / realphoto / map: 上記を適用。舞台がロシア（既定含む）のプロンプトは
  末尾に "Russian setting." を含む1文で書き切る。excerpt が別の国を明示する場合は
  その国の設定を末尾に書く（例: "Libyan setting."）
- diagram / chart: **背景は無地**（"plain solid light background" を明記）。
  ロシアの景観・街並みを図解の背景に入れることは**禁止**（読みやすさ優先）。
  適用するのは上記の通貨ルールと、人物アイコンを描く場合の外見のみ。
  末尾の1文は "Clean plain background, no scenery." にする
""",
}


NO_TEXT_BLOCK = """
【文字なし版（テロップ用・強制適用）】
このジョブは動画側でテロップを載せるため、**画像内の文字を全て禁止**する。
- ルール2・3の allowed_terms 例外は**無効**（allowed_terms に語があっても画像内に入れない）
- 全プロンプトに "No text in image. Purely visual, no labels, no numbers, no captions,
  no readable signage." を必ず含める
- 実写風でも、看板・標識の文字が読めない構図・ぼかしにする（"incidental signage must be illegible"）
"""


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
    worldview_preset: str = "",
    no_text_mode: bool = False,
) -> list:
    """1 バッチ（10 件程度）の視覚化ポイントを英文プロンプト化"""
    user_block = _build_user_block(user_instructions)
    worldview_block = WORLDVIEW_PRESETS.get((worldview_preset or "").strip(), "")
    if no_text_mode:
        worldview_block = worldview_block + NO_TEXT_BLOCK
    excerpts_json = json.dumps(excerpts_batch, ensure_ascii=False, indent=2)

    system = (
        "You are a visual director who converts Japanese manuscript excerpts "
        "into precise English image prompts for an image generation AI. "
        "Each prompt MUST faithfully represent its source excerpt, and ONLY that excerpt — "
        "never mix content from the other items in the batch. "
        "If a standing worldview style block is provided, it is a MANDATORY style layer for "
        "EVERY prompt (character appearance, setting, currency style): apply it even though "
        "those style terms do not appear in the excerpt — style is not 'extra information' "
        "and never conflicts with source fidelity. "
        "Return only a JSON array. No markdown, no commentary."
    )

    query = f"""以下は動画原稿「{title}」から抽出した{len(excerpts_batch)}個の視覚化ポイントです。
各項目に**厳密に対応する**英文画像プロンプトを作成してください。

視覚化ポイント:
{excerpts_json}
{user_block}{worldview_block}

【必須ルール】
1. プロンプトは英語で記述（画像生成モデル向け）
2. **画像内テキストの厳格制約**: もし画像内に日本語テキストを入れる場合、**allowed_terms に登場する語句のみ**使うこと。それ以外の地名・人名・数値・補足ラベルは**絶対に追加しない**。
3. allowed_terms が**空の場合でも**、excerpt 内に実在する固有名詞・数値・年代があれば、
   その中から**1〜3語を画像内ラベルとして必ず使う**（excerpt に無い語は絶対禁止）。
   使った語は出力 JSON の "used_labels" に原文ママで列挙すること。
   excerpt にも適切な語が無い場合のみ、画像内テキストなし（"no text in image" と明記）
4. 画像にタイトル文字は不要（"no title text", "no heading" を明記）
5. **16:9 横長**（"16:9 aspect ratio, landscape orientation"）
6. **シンプルでわかりやすい**仕上がり（情報過多にしない）
7. 内容に応じてイラストのタッチを変える:
   - illustration: 水彩風 / フラット / 線画 / 切り絵 / 3D風 / コミック風 / ミニマルから最適なものを選ぶ
   - realphoto: **実写風の写真**（photorealistic photograph, documentary quality）。都市・建物・施設・
     インフラ・事件・戦争・人々の生活など物理的シーンをリアルな写真として描く。イラストにしないこと
   - map: 航空写真風（aerial / satellite imagery style）。**地名ラベルは allowed_terms にあるもののみ**、なければラベルなし
   - diagram: 概念図・フロー図（矢印とボックス、3〜5要素まで）
   - chart: 棒グラフ・円グラフ・推移グラフ（要素は3〜5個まで、数値は **allowed_terms にあるもののみ**）
8. カラフル可（パステル・ビビッド・モノトーンなど自由）
9. **excerpt と allowed_terms に登場しない情報は絶対にプロンプトに含めない**（推測・補完・常識補足はすべて禁止）。
   ただし【世界観】ブロックがある場合、その**スタイル指定（人物の外見・舞台・通貨表現）だけは例外**で、
   全プロンプトに必ず適用する（スタイルは「情報」ではない）
10. 各プロンプトは互いに**異なるビジュアル**にする（同じ構図の連発禁止）
11. **シーン混入の禁止**: 各プロンプトは**その項目（index）の excerpt だけ**から作る。
    バッチ内の他の項目は「別のシーン」であり文脈ではない。他項目の人物・地名・数値・
    キーワードを混ぜたら不合格
12. **フロー・矢印の順序を明示**（diagram / フローを含む画像すべて）:
    - excerpt に書かれた因果・時系列の順序どおりに要素を並べ、**矢印の始点と終点を
      英語で書き切る**こと（例: "left-to-right flow: A → B → C", "arrow FROM the factory
      TO the store"）。「AがBになる」なら矢印は必ず A→B（逆向きは不合格）
    - 順序が excerpt から読み取れない場合は、矢印を使わない構図（並置・対比）にする

【画像内テキストの記述例】
- allowed_terms = ["東京", "100億円"] の場合:
  "The only Japanese text allowed in this image is exactly: 東京, 100億円. Do NOT add any other text, labels, numbers, or annotations."
- allowed_terms = [] の場合:
  "No text in image. Purely visual, no labels, no numbers, no captions."

【出力JSON形式】
JSON配列のみで返すこと（マークダウン禁止）:
[
  {{
    "index": (元のindexをそのまま使う),
    "prompt": "英語プロンプト（上記テキスト制約を必ず含めること）",
    "section": "セクション名",
    "excerpt": "元の抜粋（そのまま）",
    "type": "元のtype（そのまま）",
    "keypoint": "元のkeypoint（そのまま）",
    "allowed_terms": (元のallowed_termsをそのまま),
    "used_labels": ["画像内ラベルに使った語（excerpt内の原文ママのみ・無ければ空配列）"]
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
            # 入力フィールドで補完（allowed_terms は入力側を必ず優先 = ハルシネーション排除）
            p.setdefault("section", ex.get("section", ""))
            p.setdefault("excerpt", ex.get("excerpt", ""))
            p.setdefault("type", ex.get("type", "illustration"))
            p.setdefault("keypoint", ex.get("keypoint", ""))
            ex_terms = ex.get("allowed_terms", [])
            if not ex_terms:
                # 安全網: 抽出段で allowed_terms が欠けても（大量件数時の出力切り詰め等）、
                # プロンプト側が excerpt から選んだラベルを機械検証して採用する。
                # excerpt の部分文字列であることを検証するのでハルシネーションは混入しない。
                exc = ex.get("excerpt", "") or ""
                ex_terms = [s for s in (p.get("used_labels") or [])
                            if isinstance(s, str) and s.strip() and s in exc][:3]
            p["allowed_terms"] = ex_terms  # 元データ優先 + 検証済みラベルで補完
            if no_text_mode:
                p["no_text"] = True  # generator 側の最終テキスト方針にも波及させる
            merged.append(p)
        else:
            # フォールバック: 簡易プロンプトを生成（テキストなし安全モード）
            ex_text = ex.get("excerpt", "")[:100]
            t = ex.get("type", "illustration")
            fallback_prompt = (
                f"A {t} representing: {ex_text}. "
                "Simple, clear visual style. "
                "No text in image. Purely visual, no labels, no numbers. "
                "No title text. 16:9 landscape orientation."
            )
            merged.append({
                "index": idx,
                "prompt": fallback_prompt,
                "section": ex.get("section", ""),
                "excerpt": ex.get("excerpt", ""),
                "type": t,
                "keypoint": ex.get("keypoint", ""),
                "allowed_terms": ex.get("allowed_terms", []),
                **({"no_text": True} if no_text_mode else {}),
            })
    return merged


def generate_all_prompts(
    client: anthropic.Anthropic,
    excerpts: list,
    title: str,
    user_instructions: str = "",
    worldview_preset: str = "",
    no_text_mode: bool = False,
    max_workers: int = 5,
    log: Optional[Callable] = None,
) -> list:
    """全視覚化ポイントを並列バッチで英文プロンプト化"""
    log = log or (lambda *a, **kw: None)

    # 10 件ずつバッチに分割
    batches = []
    for i in range(0, len(excerpts), BATCH_SIZE):
        batches.append(excerpts[i:i + BATCH_SIZE])

    log("prompter", f"{len(excerpts)} 件を {len(batches)} バッチに分割（同時 {max_workers} 並列 / モデル {CLAUDE_MODEL}）")
    if (worldview_preset or "").strip() in WORLDVIEW_PRESETS:
        log("prompter", f"世界観プリセット適用: {worldview_preset}（人物・背景・通貨を強制指定）")
    else:
        log("prompter", "世界観プリセット: なし（画面に選択欄が無い場合はページを再読み込み）")
    if no_text_mode:
        log("prompter", "文字なし版（テロップ用）: ON — 画像内テキストを全面禁止")

    all_results = [None] * len(excerpts)
    completed_batches = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_batch = {
            executor.submit(generate_prompts_batch, client, batch, title,
                            user_instructions, worldview_preset, no_text_mode): idx
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
