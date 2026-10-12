#!/usr/bin/env python3
"""候補（複数案）を1枚ずつの生成項目へ展開する（2026-09-24）。

- single: 従来どおり抜粋1つにつき1枚。index・ファイル名（diagram_004.png）も従来と同じ。
- pair:   同じ種類で構図違いの2案（diagram_004_a.png / _b）。AIが2案目を返さない時は同じ指示で2回
          生成する（画像AIは毎回違う絵を返すので、従来の「2回生成して選ぶ」と同じ効果になる）。
- mixed:  型違いで2〜3案。地図の案は画像AIを使わず地図データで描く。

地図は実データで描くと毎回同じ絵になるため、地図の抜粋は案を増やさない（1枚）。
各項目は一意の index（画像ごとの通し番号）、抜粋ごとの group、案の variant を持つ。
"""

from generator import image_filename
from prompter import TYPE_LABELS

LETTERS = "abc"
VALID_TYPES = set(TYPE_LABELS)


def _fallback_prompt(item: dict, image_type: str) -> str:
    excerpt = (item.get("excerpt") or "")[:100]
    return (f"A {image_type} representing: {excerpt}. Simple, clear visual style. "
            "No text in image. Purely visual, no labels, no numbers. No title text. 16:9 landscape orientation.")


def _single_spec(item: dict, use_data_map: bool) -> dict:
    image_type = item.get("type") if item.get("type") in VALID_TYPES else "illustration"
    if image_type == "map" and use_data_map:
        return {"type": "map", "map_spec": item.get("map_spec") or {}}
    prompt = item.get("prompt") if isinstance(item.get("prompt"), str) and item.get("prompt").strip() else ""
    return {"type": image_type, "prompt": prompt or _fallback_prompt(item, image_type)}


def _variants(item: dict) -> list:
    raw = item.get("variants")
    return [v for v in raw if isinstance(v, dict)] if isinstance(raw, list) else []


def expand(prompts: list, candidate_mode: str = "single", map_mode: str = "ai",
           map_enabled: bool = True, no_text: bool = False) -> list:
    use_data_map = map_mode == "data" and map_enabled
    entries = []
    for item in sorted(prompts, key=lambda p: p.get("index", 0)):
        group = item.get("index")
        single = _single_spec(item, use_data_map)
        specs = [single]
        if candidate_mode == "pair" and single["type"] != "map":
            texts = [v["prompt"] for v in _variants(item)
                     if isinstance(v.get("prompt"), str) and v["prompt"].strip()][:2]
            texts = texts or [single["prompt"]]
            while len(texts) < 2:
                texts.append(texts[0])
            specs = [{"type": single["type"], "prompt": text} for text in texts]
        elif candidate_mode == "mixed":
            chosen, seen = [], set()
            for variant in _variants(item):
                image_type = variant.get("type") if variant.get("type") in VALID_TYPES else None
                if not image_type or image_type in seen:
                    continue
                if image_type == "map" and use_data_map:
                    chosen.append({"type": "map", "map_spec": variant.get("map_spec") or item.get("map_spec") or {}})
                elif isinstance(variant.get("prompt"), str) and variant["prompt"].strip():
                    chosen.append({"type": image_type, "prompt": variant["prompt"]})
                else:
                    continue
                seen.add(image_type)
                if len(chosen) == 3:
                    break
            specs = chosen or [single]

        base = {key: item[key] for key in ("section", "excerpt", "keypoint", "allowed_terms", "used_labels")
                if key in item}
        if no_text or item.get("no_text"):
            base["no_text"] = True
        for position, spec in enumerate(specs):
            variant = LETTERS[position] if len(specs) > 1 else ""
            entry = dict(base, index=len(entries) + 1, group=group, variant=variant,
                         variant_label=TYPE_LABELS.get(spec["type"], ""), type=spec["type"],
                         prompt=spec.get("prompt", ""), filename=image_filename(group, variant))
            if "map_spec" in spec:
                entry["render"] = "map"
                entry["map_spec"] = spec["map_spec"]
            entries.append(entry)
    return entries


def plan_counts(entries: list) -> dict:
    groups = {e.get("group") for e in entries}
    maps = sum(1 for e in entries if e.get("render") == "map")
    return {"groups": len(groups), "images": len(entries), "maps": maps, "ai_images": len(entries) - maps}
