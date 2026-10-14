"""地図の出しすぎ対策と「別の種類で作り直す」の検証（2026-10-14）。"""
import csv
import io
import json
from pathlib import Path
import sys
import threading
import zipfile

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as appmod
import candidates
import extractor
import generator
import image_edit
import prompter
import retype
import utils


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "test-password")
    monkeypatch.setattr(appmod, "_jobs", {})
    monkeypatch.setattr(appmod, "_job_logs", {})
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    return client


class InlineThread:
    def __init__(self, target=None, args=(), **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def png(path: Path, color="white"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (160, 90), color).save(path)
    return path


def map_job(tmp_path, provider="gpt-image", items=None):
    """地図1枚・図解1枚の完成済みジョブ（single）。"""
    root = tmp_path / "20261014_120000_abcdef"
    items = items or [
        {"index": 1, "group": 1, "variant": "", "type": "map", "render": "map", "map_spec": {"focus": ["EST"]},
         "filename": "diagram_001.png", "excerpt": "首都にある像を郊外の墓地へ移すことに決めました。",
         "section": "第2章", "keypoint": "像を郊外の墓地へ移す", "allowed_terms": ["像"], "map_alt": "realphoto"},
        {"index": 2, "group": 2, "variant": "", "type": "diagram", "prompt": "flow", "filename": "diagram_002.png",
         "excerpt": "制度の仕組みを説明します。", "section": "第2章", "keypoint": "制度の仕組み", "allowed_terms": []},
    ]
    for item in items:
        png(root / "images" / item["filename"])
    utils.save_json(root / "prompts.json", {"items": items})
    utils.save_json(root / "request.json", {"provider": provider, "openai_model": "gpt-image-2.5-flare",
                                           "openai_quality": "medium", "gemini_model": "gemini-nano-banana-2.1",
                                           "worldview_preset": "roshia", "no_text_mode": False,
                                           "user_instructions": "", "candidate_mode": "single", "map_mode": "data"})
    utils.save_json(root / "manifest.json", {"title": "テスト回", "provider": provider})
    utils.save_json(root / "job.json", {"status": "completed", "title": "テスト回"})
    return root


# ===== 抽出：地名が出るだけで地図にしない =====
def points(types, **extra):
    return [dict({"index": i, "excerpt": f"抜粋{i}", "type": t}, **extra) for i, t in enumerate(types, start=1)]


def test_map_share_is_limited_and_location_maps_are_kept():
    items = points(["map"] * 5 + ["diagram"] * 5)
    items[0].update(excerpt="エストニアとラトビアはロシアと国境を接しています", map_focus="三国とロシアの国境")
    items[3].update(excerpt="回廊は二つの国に挟まれています", map_focus="回廊の位置")
    items[1].update(map_alt="realphoto")
    changed = extractor.limit_maps(items)
    assert changed == 3  # 10項目の2割＝2枚まで
    assert [i["type"] for i in items[:5]] == ["map", "realphoto", "illustration", "map", "illustration"]
    assert items[1]["map_limited"] is True and "map_limited" not in items[0]


def test_map_limit_is_left_to_explicit_map_instructions():
    items = points(["map"] * 5 + ["diagram"] * 5)
    assert extractor.limit_maps(items, user_instructions="地図を多めに入れてください") == 0
    assert sum(i["type"] == "map" for i in items) == 5
    small = points(["map", "map", "diagram"])
    assert extractor.limit_maps(small) == 0  # 少ない時も2枚までは残す


def test_extraction_explains_map_judgment_and_cleans_map_fields(monkeypatch):
    sent = []
    reply = [
        {"index": 1, "excerpt": "首都にある像を郊外の墓地へ移すことに決めました。", "type": "realphoto",
         "map_focus": "誤って付いた", "map_alt": "diagram", "keypoint": "像を郊外の墓地へ移す", "allowed_terms": ["像"]},
        {"index": 2, "excerpt": "回廊は二つの国に挟まれています。", "type": "map", "map_focus": " 回廊の位置 ",
         "map_alt": "map", "keypoint": "回廊は二つの国に挟まれて", "allowed_terms": ["回廊"]},
    ]
    monkeypatch.setattr(extractor, "claude_query", lambda client, query, system, **kw: sent.append(query) or json.dumps(reply, ensure_ascii=False))
    result = extractor.extract_visual_points(None, "原稿" * 60, {"title": "t", "sections": [], "keywords": []}, target_count=2)
    assert "地名・国名・都市名が出てくるだけでは map にしない" in sent[0]
    assert "昔の国・昔の国境" in sent[0] and "map_focus" in sent[0]
    assert "map_focus" not in result[0] and "map_alt" not in result[0]  # 地図以外には持たせない
    assert result[1]["map_focus"] == "回廊の位置" and result[1]["map_alt"] == ""  # 代わりの種類に地図は不可


def test_supplement_request_also_has_the_type_rules(monkeypatch):
    sent = []
    replies = iter([json.dumps([{"index": 1, "excerpt": "a", "type": "diagram"}]),
                    json.dumps([{"index": 2, "excerpt": "b", "type": "map"}])])
    monkeypatch.setattr(extractor, "claude_query", lambda client, query, system, **kw: sent.append(query) or next(replies))
    extractor.extract_visual_points(None, "原稿" * 60, {"title": "t"}, target_count=2)
    assert len(sent) == 2 and "map にするかの決め方" in sent[1]


def test_mixed_candidates_only_add_maps_for_location_points():
    block = prompter.CANDIDATE_BLOCKS["mixed"]
    assert "位置関係" in block and "国名・都市名が出てくるだけ" in block


def test_prompter_carries_map_judgment_and_marks_fallback(monkeypatch):
    ex = [{"index": 3, "excerpt": "回廊の位置", "type": "map", "map_focus": "回廊の位置", "map_alt": "diagram",
           "allowed_terms": []}]
    monkeypatch.setattr(prompter, "claude_query", lambda *a, **kw: json.dumps([{"index": 3, "map_spec": {"focus": ["LTU"]}}]))
    designed = prompter.generate_prompts_batch(None, ex, "t", map_mode="data")
    assert designed[0]["map_alt"] == "diagram" and designed[0]["map_focus"] == "回廊の位置"
    assert not designed[0].get("fallback")
    monkeypatch.setattr(prompter, "claude_query", lambda *a, **kw: "[]")
    assert prompter.generate_prompts_batch(None, ex, "t")[0]["fallback"] is True


def test_candidates_keep_map_alternative_for_the_screen():
    entries = candidates.expand([{"index": 1, "type": "map", "map_spec": {"focus": ["EST"]}, "excerpt": "e",
                                  "map_alt": "realphoto", "map_focus": "f"}], "single", "data")
    assert entries[0]["map_alt"] == "realphoto" and entries[0]["map_focus"] == "f"


# ===== 別の種類で作り直す =====
def test_plan_and_settings_come_from_the_saved_job(tmp_path):
    root = map_job(tmp_path)
    item = retype.plan_item(root, "diagram_001__e2.png")  # 版を指しても元の候補をたどる
    assert item["filename"] == "diagram_001.png" and retype.original_type(item) == "map"
    settings = retype.job_settings(root)
    assert settings["provider"] == "gpt-image" and settings["worldview_preset"] == "roshia"
    assert settings["title"] == "テスト回" and retype.model_label("realphoto", settings) == "gpt-image-2.5-flare"
    assert retype.model_label("map", settings) == "地図データ" and retype.required_key("map", settings) == ""
    with pytest.raises(retype.RetypeError):
        retype.plan_item(root, "diagram_009.png")
    (root / "request.json").unlink()
    utils.save_json(root / "manifest.json", {"title": "テスト回"})
    with pytest.raises(retype.RetypeError, match="設定"):
        retype.job_settings(root)  # 古い設定が無ければ、別の設定で代用しない


def test_design_uses_the_job_worldview_and_new_type(tmp_path, monkeypatch):
    root = map_job(tmp_path)
    calls = []

    def fake_batch(client, batch, title, user_instructions="", worldview_preset="", no_text_mode=False,
                   candidate_mode="single", map_mode="ai", cancel_check=None):
        calls.append((batch, title, worldview_preset, candidate_mode, map_mode))
        return [{"index": 1, "prompt": "A documentary photo of a bronze statue. Estonian setting."}]

    monkeypatch.setattr(prompter, "generate_prompts_batch", fake_batch)
    item = retype.plan_item(root, "diagram_001.png")
    entry = retype.design(item, "realphoto", retype.job_settings(root), "diagram_001__e1.png", client=object())
    batch, title, worldview, mode, map_mode = calls[0]
    assert batch[0]["type"] == "realphoto" and batch[0]["excerpt"].startswith("首都にある像")
    assert (title, worldview, mode, map_mode) == ("テスト回", "roshia", "single", "ai")
    assert entry["filename"] == "diagram_001__e1.png" and entry["type"] == "realphoto" and "render" not in entry
    monkeypatch.setattr(prompter, "generate_prompts_batch",
                        lambda *a, **kw: [{"index": 1, "prompt": "x", "fallback": True}])
    with pytest.raises(retype.RetypeError, match="ASTRA"):
        retype.design(item, "diagram", retype.job_settings(root), "diagram_001__e2.png", client=object())


def test_design_to_map_uses_map_data(tmp_path, monkeypatch):
    root = map_job(tmp_path)
    seen = {}

    def fake_batch(client, batch, *args, map_mode="ai", **kwargs):
        seen["map_mode"] = map_mode
        return [{"index": 1, "map_spec": {"focus": ["LTU"]}}]

    monkeypatch.setattr(prompter, "generate_prompts_batch", fake_batch)
    item = retype.plan_item(root, "diagram_002.png")
    entry = retype.design(item, "map", retype.job_settings(root), "diagram_002__e1.png", client=object())
    assert seen["map_mode"] == "data" and entry["render"] == "map" and entry["map_spec"] == {"focus": ["LTU"]}


def test_retype_makes_a_new_version_and_keeps_the_map(client, tmp_path, monkeypatch):
    root = map_job(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    monkeypatch.setattr(image_edit.threading, "Thread", InlineThread)
    runs = []

    def fake_run(job_dir, item, target, settings, output_name):
        runs.append((item["filename"], target, settings["provider"], output_name))
        png(Path(job_dir) / "images" / output_name, "blue")
        return True, ""

    monkeypatch.setattr(retype, "run", fake_run)
    res = client.post(f"/api/edit/{root.name}", json={"source": "diagram_001.png", "action": "retype",
                                                      "target_type": "realphoto"})
    assert res.status_code == 200, res.json
    assert runs == [("diagram_001.png", "realphoto", "gpt-image", "diagram_001__e1.png")]
    edit = client.get(f"/api/edits/{root.name}").json["edits"][0]
    assert (edit["status"], edit["action"], edit["target_type"]) == ("ok", "retype", "realphoto")
    assert edit["label"] == "実写風で作り直し" and edit["model"] == "gpt-image-2.5-flare"
    assert (root / "images/diagram_001.png").exists() and (root / "images/diagram_001__e1.png").exists()
    snapshot = client.get(f"/api/items/{root.name}").json
    assert [i["filename"] for i in snapshot["items"]] == ["diagram_001.png", "diagram_002.png"]  # 版は一覧に増やさない
    assert snapshot["edits"][0]["output"] == "diagram_001__e1.png"


@pytest.mark.parametrize("body, message", [
    ({"source": "diagram_001.png", "action": "retype", "target_type": "chart"}, "種類が不正"),
    ({"source": "diagram_001.png", "action": "retype", "target_type": "map"}, "同じ種類"),
    ({"source": "diagram_001.png", "action": "retype"}, "種類が不正"),
    ({"source": "diagram_009.png", "action": "retype", "target_type": "diagram"}, "見つからない"),
])
def test_invalid_retype_requests_are_rejected(client, tmp_path, monkeypatch, body, message):
    map_job(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    res = client.post("/api/edit/20261014_120000_abcdef", json=body)
    assert res.status_code == 400 and message in res.json["error"]


def test_retype_needs_the_key_of_the_job_provider_but_map_does_not(client, tmp_path, monkeypatch):
    root = map_job(tmp_path, provider="nanobanana")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")  # 別のプロバイダのキーでは代用しない
    res = client.post(f"/api/edit/{root.name}", json={"source": "diagram_001.png", "action": "retype",
                                                      "target_type": "diagram"})
    assert res.status_code == 400 and "GEMINI_API_KEY" in res.json["error"]
    monkeypatch.setattr(image_edit.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    ok = client.post(f"/api/edit/{root.name}", json={"source": "diagram_002.png", "action": "retype",
                                                     "target_type": "map"})
    assert ok.status_code == 200 and ok.json["edit"]["model"] == "地図データ"


def test_many_retypes_wait_in_line_without_blocking_other_edits(tmp_path, monkeypatch):
    root = map_job(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    started = []

    class Parked(InlineThread):
        def start(self):
            started.append(self)

    monkeypatch.setattr(image_edit.threading, "Thread", Parked)
    for target in ("diagram", "illustration", "realphoto"):
        image_edit.request_edit(root, "diagram_001.png", "retype", target_type=target)
    again = image_edit.request_edit(root, "diagram_001.png", "retype", target_type="diagram")
    assert again.get("duplicate") and len(started) == 3  # 同じ依頼の二重送信は1回分だけ
    for n in range(3):  # 作り直しが3件待っていても、手直しは別に3件まで受け付ける
        image_edit.request_edit(root, "diagram_002.png", "instruct", f"指示{n}")
    with pytest.raises(image_edit.EditError, match="3件まで"):
        image_edit.request_edit(root, "diagram_002.png", "remove_text")
    statuses = [e["status"] for e in image_edit.load_edits(root)]
    assert statuses == ["queued"] * 6


def test_retype_slots_bound_parallel_work(tmp_path, monkeypatch):
    root = map_job(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    monkeypatch.setattr(image_edit, "_RETYPE_SLOTS", threading.BoundedSemaphore(1))
    gate, running, peak = threading.Event(), [], []

    def slow_run(job_dir, item, target, settings, output_name):
        running.append(target)
        peak.append(len(running))
        gate.wait(5)
        png(Path(job_dir) / "images" / output_name)
        running.remove(target)
        return True, ""

    threads = []
    real_thread = threading.Thread

    def tracked(**kwargs):
        thread = real_thread(**kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(image_edit.threading, "Thread", tracked)
    for target in ("diagram", "illustration"):
        image_edit.request_edit(root, "diagram_001.png", "retype", target_type=target, retype_runner=slow_run)
    gate.set()
    for thread in threads:
        thread.join(5)
    assert max(peak) == 1
    assert [e["status"] for e in image_edit.load_edits(root)] == ["ok", "ok"]


def test_failed_retype_is_recorded_with_a_readable_reason(tmp_path, monkeypatch):
    root = map_job(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    monkeypatch.setattr(image_edit.threading, "Thread", InlineThread)
    image_edit.request_edit(root, "diagram_001.png", "retype", target_type="illustration",
                            retype_runner=lambda *a: (False, "content policy blocked: safety"))
    edit = image_edit.load_edits(root)[0]
    assert edit["status"] == "failed" and "安全確認" in edit["error"]
    assert not (root / "images" / edit["output"]).exists()


def test_adopted_zip_names_the_new_type(client, tmp_path):
    root = map_job(tmp_path)
    png(root / "images/diagram_001__e1.png", "blue")
    utils.save_json(root / "edits.json", {"version": 1, "edits": [
        {"id": "r1", "source": "diagram_001.png", "output": "diagram_001__e1.png", "action": "retype",
         "target_type": "realphoto", "label": "実写風で作り直し", "status": "ok"}]})
    utils.save_json(root / "images_progress.json", {"items": [
        {"index": 1, "group": 1, "variant": "", "variant_label": "地図", "filename": "diagram_001.png",
         "excerpt": "首都にある像を郊外の墓地へ移すことに決めました。", "keypoint": "像を郊外の墓地へ移す"}]})
    image_edit.set_adopted(root, "diagram_001__e1.png", True, 1)
    data = client.get(f"/download-adopted/{root.name}").data
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        rows = list(csv.reader(io.StringIO(zf.read("採用一覧.csv").decode("utf-8-sig"))))
    assert rows[1][2] == "diagram_001__e1.png" and rows[1][5] == "実写風" and rows[1][6] == "実写風で作り直し"


def test_version_reports_map_fit(client):
    data = client.get("/version").json
    assert data["map_fit"]["map_share_limit"] == 0.2 and data["map_fit"]["place_name_alone_is_not_map"] is True
    assert data["map_fit"]["retype_targets"] == ["diagram", "illustration", "realphoto", "map"]
    assert "retype" in data["director_tools"]["edits"] and data["map_fit"]["learns_from_retype"] is True


# ===== 直した例を次の回に活かす（素材スタジオの「ルート違い」と同じ考え方） =====
import type_feedback  # noqa: E402


def test_feedback_keeps_latest_judgment_per_sentence_and_is_sanitized(tmp_path):
    type_feedback.record(tmp_path, "首都にある「像」を\n郊外へ移した", "map", "diagram")
    type_feedback.record(tmp_path, "首都にある「像」を\n郊外へ移した", "map", "realphoto")
    type_feedback.record(tmp_path, "回廊は二つの国に挟まれている", "diagram", "map")
    type_feedback.record(tmp_path, "同じ種類は残さない", "map", "map")
    (tmp_path / type_feedback.FILE_NAME).open("a", encoding="utf-8").write("壊れた行\n")
    items = type_feedback.examples(tmp_path)
    assert items == [{"sentence": "回廊は二つの国に挟まれている", "given_type": "diagram", "correct_type": "map"},
                     {"sentence": "首都にある『像』を 郊外へ移した", "given_type": "map", "correct_type": "realphoto"}]
    block = type_feedback.prompt_block(items)
    assert "「首都にある『像』を 郊外へ移した」は map ではなく realphoto が合っていた" in block
    assert type_feedback.examples(tmp_path / "missing") == [] and type_feedback.prompt_block([]) == ""


def test_retype_request_teaches_the_next_extraction(tmp_path, monkeypatch):
    root = map_job(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    monkeypatch.setattr(image_edit.threading, "Thread", lambda **kw: type("T", (), {"start": lambda self: None})())
    image_edit.request_edit(root, "diagram_001.png", "retype", target_type="realphoto")
    image_edit.request_edit(root, "diagram_001.png", "retype", target_type="realphoto")  # 二重送信は1件
    items = type_feedback.examples(tmp_path)
    assert items == [{"sentence": "首都にある像を郊外の墓地へ移すことに決めました。", "given_type": "map",
                      "correct_type": "realphoto"}]
    assert len((tmp_path / type_feedback.FILE_NAME).read_text(encoding="utf-8").splitlines()) == 1
    sent = []
    monkeypatch.setattr(extractor, "claude_query",
                        lambda client, query, system, **kw: sent.append(query) or json.dumps([{"index": 1, "excerpt": "a", "type": "diagram"}]))
    extractor.extract_visual_points(None, "原稿" * 60, {"title": "t"}, target_count=1, type_examples=items)
    assert "過去にディレクターが種類を直した例" in sent[0] and "map ではなく realphoto" in sent[0]
