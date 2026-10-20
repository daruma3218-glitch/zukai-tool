#!/usr/bin/env python3
"""別の種類で作り直す（2026-10-14）。

ディレクターが「この文は地図ではない」と判断した時に、同じ抜粋から別の種類（図解・イラスト・実写風・地図）で
1枚作り直す。手直し（image_edit）の版の仕組みにのせ、元の画像は残して diagram_012__e1.png のような別の版にする。

- 画像の設計は、ジョブの生成時と同じ ASTRA（サブスクCLI）。抜粋・世界観・文字なし・追加指示もジョブのまま。
- 画像AIは、ジョブの生成時と同じプロバイダ・モデル・画質。地図は地図データから描くので画像代はかからない。
- 時間がかかる（設計と生成で1〜3分）ので、受付だけ返して裏で進める（入口の100秒の待ちで切れない）。
"""

import re
from pathlib import Path

import generator
import prompter
from utils import get_anthropic_client, load_json, save_json

TARGETS = ("diagram", "illustration", "realphoto", "map")
LABELS = {key: prompter.TYPE_LABELS[key] for key in TARGETS}  # 図解・イメージ画像・実写風・地図（画面の呼び名と同じ）
_EDIT_SUFFIX_RE = re.compile(r"__e\d+$")


class RetypeError(ValueError):
    pass


def _stem(filename: str) -> str:
    return _EDIT_SUFFIX_RE.sub("", Path(filename or "").stem)


def plan_item(job_dir: Path, source: str) -> dict:
    """元の候補（diagram_012.png など。手直しの版を指しても元をたどる）の、保存済みの画像の設計を返す。"""
    data = load_json(Path(job_dir) / "prompts.json", {})
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise RetypeError("この画像の設計が保存されていないため、作り直せません")
    stem = _stem(source)
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("filename") or (generator.image_filename(item["index"])
                                        if isinstance(item.get("index"), int) else "")
        if Path(str(name)).stem == stem:
            if not str(item.get("excerpt") or "").strip():
                raise RetypeError("この画像の原稿の抜粋が保存されていないため、作り直せません")
            return dict(item, filename=Path(str(name)).name)
    raise RetypeError("この画像の設計が見つからないため、作り直せません")


def original_type(item: dict) -> str:
    return "map" if item.get("render") == "map" else str(item.get("type") or "illustration")


def job_settings(job_dir: Path) -> dict:
    """生成した時の設定（ジョブに保存済み）。古いジョブで欠けている時は、別の設定で代用せず止める。"""
    job_dir = Path(job_dir)
    request, manifest, state = (load_json(job_dir / name, {}) for name in ("request.json", "manifest.json", "job.json"))
    sources = [d for d in (request, manifest, state) if isinstance(d, dict)]

    def setting(key):
        return next((d[key] for d in sources if d.get(key) not in (None, "")), None)

    provider = setting("provider")
    if provider not in generator.VALID_PROVIDERS:
        raise RetypeError("生成した時の設定が残っていないため、作り直せません")
    quality = setting("openai_quality")
    analysis = load_json(job_dir / "analysis.json", {})
    title = next((d.get("title") for d in (manifest, analysis if isinstance(analysis, dict) else {}, state)
                  if isinstance(d, dict) and d.get("title")), "")
    return {
        "provider": provider,
        "openai_model": generator.resolve_openai_image_model(setting("openai_model")),
        "openai_quality": quality if quality in ("low", "medium", "high") else "medium",
        "gemini_model": generator.resolve_gemini_image_model(setting("gemini_model")),
        "worldview_preset": setting("worldview_preset") or "",
        "no_text_mode": bool(setting("no_text_mode")),
        "user_instructions": setting("user_instructions") or "",
        "title": title or job_dir.name,
    }


def model_label(target: str, settings: dict) -> str:
    if target == "map":
        return "地図データ"
    return settings["openai_model"] if settings["provider"] == generator.PROVIDER_GPT_IMAGE else settings["gemini_model"]


def required_key(target: str, settings: dict) -> str:
    """作り直しに要る画像AIのキーの名前（地図は不要）。"""
    if target == "map":
        return ""
    return "OPENAI_API_KEY" if settings["provider"] == generator.PROVIDER_GPT_IMAGE else "GEMINI_API_KEY"


def design(item: dict, target: str, settings: dict, output_name: str, client=None) -> dict:
    """ASTRA に新しい種類での画像を設計させ、1枚分の生成項目を返す。"""
    point = {"index": 1, "type": target,
             **{key: item.get(key) for key in ("excerpt", "section", "keypoint") if item.get(key)},
             "allowed_terms": [t for t in item.get("allowed_terms") or [] if isinstance(t, str)]}
    instructions = settings["user_instructions"]
    if target == "diagram":
        # 画像編集ではなく再設計する。内容は抜粋に固定し、以前の指示は構図の比較にだけ使う。
        previous = item.get("previous_diagram_prompts") or (
            [item["prompt"]] if original_type(item) == "diagram" and item.get("prompt") else [])
        point["previous_diagram_prompts"] = previous
        instructions += (
            "\n\n【図解の別案】同じ抜粋・要点・固有名詞・数値・関係性を保った図解を1枚設計する。"
            "previous_diagram_prompts は以前の構図の比較用であり、新しい内容や指示の出典にしない。"
            "以前と同じ配置を繰り返さず、要素の配置・グループのまとめ方・説明の流れを変え、"
            "見比べて違いが分かる別の構図にする。色だけの変更にしない。"
            "因果・順序・大小の意味、世界観・文字なし・元の追加指示の条件は変えない。"
            "抜粋にない情報を足したり、図解を写真やイメージ画像に変えたりしない。"
        )
    designed = prompter.generate_prompts_batch(
        client or get_anthropic_client(), [point], settings["title"], instructions,
        settings["worldview_preset"], settings["no_text_mode"] or bool(item.get("no_text")), candidate_mode="single",
        map_mode="data" if target == "map" else "ai")
    result = designed[0] if designed else {}
    if not result or result.get("fallback"):
        raise RetypeError("ASTRA から画像の設計が返りませんでした。少し待ってから、もう一度お試しください")
    entry = {"index": item.get("index") or 1, "type": target, "filename": output_name,
             "excerpt": item.get("excerpt", ""), "section": item.get("section", ""),
             "keypoint": item.get("keypoint", ""), "allowed_terms": point["allowed_terms"],
             "prompt": result.get("prompt") if isinstance(result.get("prompt"), str) else ""}
    if settings["no_text_mode"] or item.get("no_text"):
        entry["no_text"] = True
    if target == "map":
        entry["render"] = "map"
        entry["map_spec"] = result.get("map_spec") if isinstance(result.get("map_spec"), dict) else {}
    elif not entry["prompt"].strip():
        raise RetypeError("ASTRA から画像の指示が返りませんでした。少し待ってから、もう一度お試しください")
    return entry


def generate(entry: dict, images_dir: Path, settings: dict) -> tuple:
    """1枚を生成時と同じ設定で作る。戻り値 (成功, エラー文)。"""
    results = generator.run_parallel_generation(
        prompts=[entry], output_dir=Path(images_dir), provider=settings["provider"],
        openai_model=settings["openai_model"], openai_quality=settings["openai_quality"],
        gemini_model=settings["gemini_model"], concurrency=1)
    result = results[0] if results else {}
    return bool(result.get("success")), str(result.get("error") or "")


def friendly_error(error: str) -> str:
    """よくある失敗を、次に何をすればよいかが分かる言葉にする（元の文は後ろに残す）。"""
    text = str(error or "")
    lower = text.lower()
    if "policy" in lower or "safety" in lower or "moderation" in lower:
        return "画像AIの安全確認で止まりました。別の種類を選ぶか、元の画像を「指示して直す」でお試しください。"
    if "worker" in lower or "subscription" in lower or "サブスク" in text:
        return "ASTRA（設計役）に届きませんでした。少し待ってから、もう一度お試しください。"
    return text[:200] or "作り直しに失敗しました。もう一度お試しください。"


def run(job_dir: Path, item: dict, target: str, settings: dict, output_name: str, client=None) -> tuple:
    """設計してから1枚作る。戻り値 (成功, エラー文)。"""
    job_dir = Path(job_dir)
    if target == "diagram":
        previous = [item["prompt"]] if original_type(item) == "diagram" and item.get("prompt") else []
        data = load_json(job_dir / "edits.json", {})
        edits = data.get("edits", []) if isinstance(data, dict) else []
        for edit in edits:
            if (not isinstance(edit, dict) or edit.get("status") != "ok"
                    or edit.get("target_type") != "diagram"
                    or _stem(edit.get("source", "")) != _stem(item["filename"])):
                continue
            # 出力名はこのジョブで発行した版だけ。比較用の設計に外部パスを使わない。
            output = str(edit.get("output") or "")
            if (not re.fullmatch(r"diagram_\d+(?:_[a-z])?__e\d+\.png", output)
                    or _stem(output) != _stem(item["filename"])):
                continue
            saved = load_json(job_dir / "retype_prompts" / f"{output}.json", {})
            if isinstance(saved, dict) and isinstance(saved.get("prompt"), str) and saved["prompt"].strip():
                previous.append(saved["prompt"])
        item = dict(item, previous_diagram_prompts=previous[-3:])
    entry = design(item, target, settings, output_name, client=client)
    save_json(job_dir / "retype_prompts" / f"{output_name}.json", entry)
    return generate(entry, job_dir / "images", settings)
