"""候補の展開・1枚ずつの手直し・採用・採用分ZIP・既定モデルの検証。"""
import csv
import io
from pathlib import Path
import sys
import zipfile
from unittest import mock

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as appmod
import candidates
import generator
import image_edit
import pipeline
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


def png(path: Path, size=(160, 90), left="red", right="blue"):
    img = Image.new("RGB", size, right)
    for x in range(size[0] // 2):
        for y in range(size[1]):
            img.putpixel((x, y), Image.new("RGB", (1, 1), left).getpixel((0, 0)))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def job_with_images(tmp_path, *names, status="completed"):
    root = tmp_path / "20260924_120000"
    for name in names:
        png(root / "images" / name)
    utils.save_json(root / "job.json", {"status": status, "title": "テスト回"})
    return root


# ===== 候補の展開 =====
def groups(n, **extra):
    return [dict({"index": i, "excerpt": f"抜粋{i}", "section": "第1章", "type": "diagram",
                  "prompt": f"prompt {i}"}, **extra) for i in range(1, n + 1)]


def test_single_keeps_old_numbers_and_filenames():
    entries = candidates.expand(groups(3), "single", "data")
    assert [(e["index"], e["group"], e["filename"]) for e in entries] == [
        (1, 1, "diagram_001.png"), (2, 2, "diagram_002.png"), (3, 3, "diagram_003.png")]
    assert all(e["variant"] == "" and "render" not in e for e in entries)


def test_pair_makes_two_candidates_even_without_second_prompt():
    items = groups(2)
    items[0]["variants"] = [{"variant": "a", "prompt": "wide view"}, {"variant": "b", "prompt": "close up"}]
    entries = candidates.expand(items, "pair", "data")
    assert [e["filename"] for e in entries] == ["diagram_001_a.png", "diagram_001_b.png",
                                                "diagram_002_a.png", "diagram_002_b.png"]
    assert [e["prompt"] for e in entries[:2]] == ["wide view", "close up"]
    assert entries[2]["prompt"] == entries[3]["prompt"] == "prompt 2"  # 同じ指示で2回＝従来の2回生成と同じ
    assert [e["index"] for e in entries] == [1, 2, 3, 4]


def test_map_excerpt_is_one_data_map_even_in_pair_mode():
    items = groups(1, type="map", map_spec={"focus": ["JPN"]})
    entries = candidates.expand(items, "pair", "data")
    assert len(entries) == 1 and entries[0]["render"] == "map" and entries[0]["map_spec"] == {"focus": ["JPN"]}
    ai_entries = candidates.expand(items, "single", "ai")
    assert "render" not in ai_entries[0]  # 従来の画像AIの地図にも戻せる


def test_mixed_uses_distinct_types_and_data_map():
    items = groups(1)
    items[0]["variants"] = [{"variant": "a", "type": "diagram", "prompt": "flow"},
                            {"variant": "b", "type": "diagram", "prompt": "dup"},
                            {"variant": "c", "type": "map", "map_spec": {"focus": ["RUS"]}},
                            {"variant": "d", "type": "realphoto", "prompt": "photo"},
                            {"variant": "e", "type": "illustration", "prompt": "fourth"}]
    entries = candidates.expand(items, "mixed", "data")
    assert [(e["variant"], e["type"]) for e in entries] == [("a", "diagram"), ("b", "map"), ("c", "realphoto")]
    assert entries[1]["render"] == "map"
    assert candidates.plan_counts(entries) == {"groups": 1, "images": 3, "maps": 1, "ai_images": 2}


def test_mixed_falls_back_to_single_when_variants_are_unusable():
    items = groups(1, variants=[{"type": "unknown"}, "bad"])
    entries = candidates.expand(items, "mixed", "data")
    assert len(entries) == 1 and entries[0]["filename"] == "diagram_001.png"


# ===== 既定モデル・編集の比率 =====
def test_default_models_are_gpt_image_25(monkeypatch):
    monkeypatch.delenv("OPENAI_IMAGE_MODEL", raising=False)
    monkeypatch.delenv("ZUKAI_EDIT_MODEL", raising=False)
    assert generator.resolve_openai_image_model() == "gpt-image-2.5-flare"
    assert generator.resolve_edit_model() == "gpt-image-2.5-sunburst"
    monkeypatch.setenv("OPENAI_IMAGE_MODEL", "gpt-image-2")
    assert generator.resolve_openai_image_model() == "gpt-image-2"  # 環境変数で従来に戻せる
    assert generator.resolve_openai_image_model("gpt-image-2.5-sunburst") == "gpt-image-2.5-sunburst"


def test_edit_padding_keeps_original_frame():
    img = Image.new("RGB", (1600, 900), "white")
    canvas, box = generator._pad_to_ratio(img, 1.5)
    assert abs(canvas.size[0] / canvas.size[1] - 1.5) < 0.01
    assert (box[2] - box[0], box[3] - box[1]) == (1600, 900)


def test_ai_edit_is_cropped_back_to_original_size(tmp_path):
    source = png(tmp_path / "src.png", size=(320, 180))

    class FakeImages:
        def edit(self, **kwargs):
            assert kwargs["model"] == "gpt-image-2.5-sunburst"
            assert kwargs["size"] == "1536x1024"
            buffer = io.BytesIO()
            Image.new("RGB", (1536, 1024), "green").save(buffer, format="PNG")
            import base64
            return mock.Mock(data=[mock.Mock(b64_json=base64.b64encode(buffer.getvalue()).decode())])

    ok, error = generator._sync_edit_image_openai(mock.Mock(images=FakeImages()), source, "remove text",
                                                  tmp_path / "out.png", model_name="gpt-image-2.5-sunburst")
    assert ok, error
    with Image.open(tmp_path / "out.png") as out:
        assert out.size == (320, 180)


def test_map_entries_are_drawn_without_image_api(tmp_path):
    entries = [{"index": 1, "group": 1, "variant": "", "type": "map", "render": "map",
                "map_spec": {"focus": ["JPN"], "highlight": [{"a3": "JPN"}]}, "excerpt": "日本",
                "filename": "diagram_001.png"}]
    with mock.patch.object(generator, "_sync_generate_image_openai",
                           side_effect=AssertionError("image API must not be called")):
        results = generator.run_parallel_generation(entries, tmp_path, provider=generator.PROVIDER_GPT_IMAGE,
                                                    openai_api_key="fixture-only", concurrency=1)
    assert results[0]["success"] and results[0]["provider"] == "map-data"
    assert (tmp_path / "diagram_001.png").exists()


# ===== 画面用データ =====
def test_snapshot_groups_candidates_and_hides_edit_files(client, tmp_path):
    root = job_with_images(tmp_path, "diagram_001_a.png", "diagram_001_b.png", "diagram_001_a__e1.png")
    utils.save_json(root / "images_progress.json", {"items": [
        {"index": 1, "group": 1, "variant": "a", "status": "ok", "filename": "diagram_001_a.png"}]})
    snap = client.get("/api/items/20260924_120000").json
    files = sorted(i["filename"] for i in snap["items"])
    assert files == ["diagram_001_a.png", "diagram_001_b.png"]
    orphan = next(i for i in snap["items"] if i["filename"] == "diagram_001_b.png")
    assert (orphan["group"], orphan["variant"]) == (1, "b")
    assert snap["available_images"] == 3  # ZIP には手直し版も入る
    assert snap["retention"]["expires_at"]


# ===== 手直し =====
def test_flip_creates_mirrored_version_and_keeps_original(client, tmp_path):
    root = job_with_images(tmp_path, "diagram_001.png")
    res = client.post("/api/edit/20260924_120000", json={"source": "diagram_001.png", "action": "flip"})
    assert res.status_code == 200, res.json
    entry = res.json["edit"]
    assert entry["status"] == "ok" and entry["output"] == "diagram_001__e1.png"
    with Image.open(root / "images/diagram_001.png") as original, Image.open(root / "images/diagram_001__e1.png") as flipped:
        assert original.getpixel((5, 5))[0] > 200 and flipped.getpixel((5, 5))[2] > 200
    second = client.post("/api/edit/20260924_120000", json={"source": "diagram_001.png", "action": "flip"}).json
    assert second["edit"]["output"] == "diagram_001__e2.png"


def test_remove_text_runs_with_sunburst_and_records_result(client, tmp_path, monkeypatch):
    job_with_images(tmp_path, "diagram_002.png")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    monkeypatch.setattr(image_edit.threading, "Thread", InlineThread)
    calls = []

    def fake_edit(client_, source, prompt, output, model_name, quality="medium"):
        calls.append((model_name, prompt))
        generator._save_png(Image.new("RGB", (160, 90), "white"), output)
        return True, ""

    monkeypatch.setattr(generator, "_sync_edit_image_openai", fake_edit)
    res = client.post("/api/edit/20260924_120000", json={"source": "diagram_002.png", "action": "remove_text"})
    assert res.status_code == 200
    edits = client.get("/api/edits/20260924_120000").json["edits"]
    assert edits[0]["status"] == "ok" and edits[0]["model"] == "gpt-image-2.5-sunburst"
    assert calls[0][0] == "gpt-image-2.5-sunburst" and "Remove every piece of text" in calls[0][1]


def test_duplicate_running_request_is_not_charged_twice(tmp_path, monkeypatch):
    root = job_with_images(tmp_path, "diagram_003.png")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    started = []

    class NeverRuns(InlineThread):
        def start(self):
            started.append(self)

    monkeypatch.setattr(image_edit.threading, "Thread", NeverRuns)
    first = image_edit.request_edit(root, "diagram_003.png", "instruct", "空を夕焼けに")
    again = image_edit.request_edit(root, "diagram_003.png", "instruct", "空を夕焼けに")
    assert again.get("duplicate") and again["id"] == first["id"]
    assert len(started) == 1
    for n in range(2):
        image_edit.request_edit(root, "diagram_003.png", "instruct", f"別の指示{n}")
    with pytest.raises(image_edit.EditError, match="3件まで"):
        image_edit.request_edit(root, "diagram_003.png", "remove_text")


def test_edit_stuck_by_restart_is_released(tmp_path, monkeypatch):
    root = job_with_images(tmp_path, "diagram_004.png")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    old = "2026-01-01T00:00:00"
    utils.save_json(root / "edits.json", {"version": 1, "edits": [
        {"id": f"s{n}", "source": "diagram_004.png", "output": f"diagram_004__e{n}.png", "action": "instruct",
         "instruction": f"x{n}", "status": "running", "created_at": old} for n in (1, 2, 3)]})
    assert all(e["status"] == "failed" and "中断" in e["error"] for e in image_edit.load_edits(root))
    monkeypatch.setattr(image_edit.threading, "Thread", lambda **kw: mock.Mock())
    entry = image_edit.request_edit(root, "diagram_004.png", "remove_text")  # 枠が空いて受け付けられる
    assert entry["status"] == "running" and entry["output"] == "diagram_004__e4.png"


@pytest.mark.parametrize("body", [{"source": "../job.json", "action": "flip"},
                                  {"source": "diagram_001.png", "action": "delete"},
                                  {"source": "diagram_001.png", "action": "instruct", "instruction": ""},
                                  {"source": "missing.png", "action": "flip"}])
def test_invalid_edit_requests_are_rejected(client, tmp_path, body):
    job_with_images(tmp_path, "diagram_001.png")
    assert client.post("/api/edit/20260924_120000", json=body).status_code == 400


def test_ai_edit_without_key_is_refused(client, tmp_path, monkeypatch):
    job_with_images(tmp_path, "diagram_001.png")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    res = client.post("/api/edit/20260924_120000", json={"source": "diagram_001.png", "action": "remove_text"})
    assert res.status_code == 400 and "OPENAI_API_KEY" in res.json["error"]


# ===== 採用 =====
def test_adopted_zip_contains_only_adopted_with_table(client, tmp_path):
    root = job_with_images(tmp_path, "diagram_001_a.png", "diagram_001_b.png", "diagram_001_b__e1.png")
    utils.save_json(root / "images_progress.json", {"items": [
        {"index": 1, "group": 1, "variant": "a", "variant_label": "図解", "status": "ok",
         "filename": "diagram_001_a.png", "excerpt": "抜粋の文", "section": "第1章"},
        {"index": 2, "group": 1, "variant": "b", "variant_label": "イメージ画像", "status": "ok",
         "filename": "diagram_001_b.png", "excerpt": "抜粋の文", "section": "第1章"}]})
    utils.save_json(root / "edits.json", {"version": 1, "edits": [
        {"id": "x", "source": "diagram_001_b.png", "output": "diagram_001_b__e1.png", "action": "flip",
         "label": "左右反転", "status": "ok"}]})
    assert client.get("/download-adopted/20260924_120000").status_code == 409
    client.post("/api/adopt/20260924_120000", json={"filename": "diagram_001_b__e1.png", "group": 1})
    client.post("/api/adopt/20260924_120000", json={"filename": "diagram_001_a.png", "group": 1})
    state = client.post("/api/adopt/20260924_120000", json={"filename": "diagram_001_a.png", "adopted": False}).json
    assert state["adopted"] == ["diagram_001_b__e1.png"]
    res = client.get("/download-adopted/20260924_120000")
    with zipfile.ZipFile(io.BytesIO(res.data)) as archive:
        names = archive.namelist()
        assert "images/diagram_001_b__e1.png" in names and "images/diagram_001_a.png" not in names
        rows = list(csv.reader(io.StringIO(archive.read("採用一覧.csv").decode("utf-8-sig"))))
    assert rows[1][:5] == ["1", "diagram_001_b__e1.png", "b", "イメージ画像", "左右反転"]
    res.close()


def test_new_routes_require_login(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "test-password")
    job_with_images(tmp_path, "diagram_001.png")
    anonymous = appmod.app.test_client()
    assert anonymous.post("/api/edit/20260924_120000", json={}).status_code == 302
    assert anonymous.post("/api/adopt/20260924_120000", json={}).status_code == 302
    assert anonymous.get("/download-adopted/20260924_120000").status_code == 302


# ===== 作成画面からの受け渡し =====
@pytest.mark.parametrize("sent, expected", [({}, ("single", "data")),
                                            ({"candidate_mode": "pair", "map_mode": "ai"}, ("pair", "ai")),
                                            ({"candidate_mode": "bogus", "map_mode": "bogus"}, ("single", "data"))])
def test_start_passes_candidate_and_map_modes(client, monkeypatch, sent, expected):
    captured = {}

    class FakePipeline:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self):
            return {"succeeded": 0, "failed": 0, "target_count": 0, "title": "t"}

    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    monkeypatch.setattr(appmod, "DiagramPipeline", FakePipeline)
    monkeypatch.setattr(appmod.threading, "Thread", InlineThread)
    data = {"manuscript_text": "テスト原稿。" * 25, "provider": "gpt-image", **sent}
    assert client.post("/start", data=data).status_code == 200
    assert (captured["candidate_mode"], captured["map_mode"]) == expected


def test_version_reports_director_tools(client):
    version = client.get("/version").json
    assert version["director_tools"]["adoption"] is True
    assert version["edit_image_model"] == "gpt-image-2.5-sunburst"


@pytest.mark.parametrize("env, expected", [(None, "gpt-image-2.5-flare"), ("gpt-image-2", "gpt-image-2")])
def test_upload_form_preselects_the_default_image_model(client, monkeypatch, env, expected):
    # 画面の初期選択も環境変数 OPENAI_IMAGE_MODEL に従う（従来モデルへ戻す切替が画面にも効く）
    if env:
        monkeypatch.setenv("OPENAI_IMAGE_MODEL", env)
    else:
        monkeypatch.delenv("OPENAI_IMAGE_MODEL", raising=False)
    html = client.get("/").get_data(as_text=True)
    assert f'<option value="{expected}" selected>' in html


def test_trimmed_job_lists_only_kept_images_without_failures(client, tmp_path):
    import retention
    root = job_with_images(tmp_path, "diagram_001_a.png", "diagram_001_b.png")
    utils.save_json(root / "images_progress.json", {"items": [
        {"index": 1, "group": 1, "variant": "a", "filename": "diagram_001_a.png", "status": "ok"},
        {"index": 2, "group": 1, "variant": "b", "filename": "diagram_001_b.png", "status": "ok"}]})
    (root / "images" / "diagram_001_a.png").unlink()  # 保存期限で消えた（採用していない案）
    (root / retention.TRIM_MARKER).write_text("{}", encoding="utf-8")
    data = client.get(f"/api/items/{root.name}").json
    assert [i["filename"] for i in data["items"]] == ["diagram_001_b.png"]
    assert all(i["status"] == "ok" for i in data["items"])
    assert data["retention"]["trimmed"] is True and data["retention"]["expires_at"] is None


def test_upload_page_shows_capacity_warning(client, tmp_path):
    import json
    from datetime import datetime
    import retention
    (tmp_path / retention.STATE_NAME).write_text(json.dumps(
        {"at": datetime.now().isoformat(), "capacity_triggered": True,
         "warning": "保存領域の91%を使っています。"}, ensure_ascii=False), encoding="utf-8")
    assert "保存領域の91%を使っています。" in client.get("/").get_data(as_text=True)
    (tmp_path / retention.STATE_NAME).write_text(json.dumps(
        {"at": "2026-01-01T00:00:00", "capacity_triggered": True, "warning": None}), encoding="utf-8")
    assert 'id="retentionNotice"' not in client.get("/").get_data(as_text=True)
