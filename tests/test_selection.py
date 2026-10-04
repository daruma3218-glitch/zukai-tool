"""Manual selection marks: persisted, authenticated, and independent of images/adoption."""
from pathlib import Path
import sys

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as appmod
import image_edit
import retention
from utils import save_json


@pytest.fixture
def selection(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "test-password")
    root = tmp_path / "selection-test"
    (root / "images").mkdir(parents=True)
    for name in ("diagram_001_a.png", "diagram_001_b.png", "diagram_001_a__e1.png"):
        with Image.new("RGB", (32, 18), "purple") as img:
            img.save(root / "images" / name)
    save_json(root / "job.json", {"status": "completed"})
    save_json(root / "manifest.json", {"items": [
        {"index": 1, "group": 1, "variant": "a", "filename": "diagram_001_a.png", "section": "第1章"},
        {"index": 2, "group": 1, "variant": "b", "filename": "diagram_001_b.png", "section": "第1章"}]})
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session.update(authenticated=True, cancel_csrf="selection-test-csrf")
    return client, root


def mark(client, filename="diagram_001_a.png", needed=True):
    return client.post("/api/selection/selection-test", json={"filename": filename, "needed": needed},
                       headers={"X-CSRF-Token": "selection-test-csrf"})


def test_marks_survive_reload_and_do_not_touch_images_adoption_or_job(selection):
    client, root = selection
    image_edit.set_adopted(root, "diagram_001_a__e1.png", True, 1)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert mark(client).json == {"needs_revision": ["diagram_001_a.png"]}
    assert mark(client, "diagram_001_b.png").status_code == 200
    snap = client.get("/api/items/selection-test").json
    assert snap["needs_revision"] == ["diagram_001_a.png", "diagram_001_b.png"]
    assert snap["adopted"] == ["diagram_001_a__e1.png"]
    assert snap["available_images"] == 3
    assert all((root / name).read_bytes() == content for name, content in before.items())
    assert mark(client, needed=False).json == {"needs_revision": ["diagram_001_b.png"]}
    assert mark(client, needed=False).status_code == 200  # explicit set, never a server-side toggle
    assert list(image_edit.load_revision_marks(root)) == ["diagram_001_b.png"]


def test_old_jobs_have_no_marks_and_reads_do_not_create_metadata(selection):
    client, root = selection
    assert client.get("/api/items/selection-test").json["needs_revision"] == []
    assert not (root / image_edit.SELECTION_NAME).exists()


def test_new_route_requires_login_and_csrf(selection):
    client, root = selection
    assert client.post("/api/selection/selection-test", json={"filename": "diagram_001_a.png", "needed": True}).status_code == 403
    with client.session_transaction() as session:
        session.clear()
    assert mark(client).status_code == 302
    assert not (root / image_edit.SELECTION_NAME).exists()


@pytest.mark.parametrize("filename,needed", [
    ("../job.json", True), ("diagram_001_a__e1.png", True), ("missing.png", True),
    (None, True), ([], True), ("diagram_001_a.png", "false"), ("diagram_001_a.png", 1),
])
def test_invalid_marks_are_rejected_without_a_write(selection, filename, needed):
    client, root = selection
    assert mark(client, filename, needed).status_code == 400
    assert not (root / image_edit.SELECTION_NAME).exists()


def test_malformed_request_or_unknown_job(selection):
    client, root = selection
    headers = {"X-CSRF-Token": "selection-test-csrf"}
    assert client.post("/api/selection/selection-test", json=[1], headers=headers).status_code == 400
    assert client.post("/api/selection/missing", json={"filename": "diagram_001_a.png", "needed": True}, headers=headers).status_code == 404


def test_corrupt_marks_are_preserved(selection):
    client, root = selection
    path = root / image_edit.SELECTION_NAME
    path.write_text("{broken", encoding="utf-8")
    assert mark(client).status_code == 400
    assert path.read_text(encoding="utf-8") == "{broken"
    assert not (root.parent / retention.LOCK_NAME).exists()


def test_retention_lock_prevents_racing_metadata_write(selection, monkeypatch):
    client, root = selection
    monkeypatch.setattr(retention, "_acquire_lock", lambda _: None)
    assert mark(client).status_code == 503
    assert not (root / image_edit.SELECTION_NAME).exists()


def test_marks_do_not_change_download_contents(selection):
    client, root = selection
    image_edit.set_adopted(root, "diagram_001_a.png", True, 1)
    before = client.get("/download-adopted/selection-test").data
    assert mark(client).status_code == 200
    import io
    import zipfile
    def contents(payload):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            return {name: archive.read(name) for name in archive.namelist()}
    assert contents(client.get("/download-adopted/selection-test").data) == contents(before)
