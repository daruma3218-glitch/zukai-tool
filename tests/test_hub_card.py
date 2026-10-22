"""制作の進行ハブの案件（Trello のカード）をジョブに残し、AI の呼び出しの記録へ渡す（2026-10-22）。"""
from pathlib import Path
import sys

import pytest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as service
import retention
import subscription_runtime as runtime
from utils import load_json


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(service, "APP_PASSWORD", "fixture")
    monkeypatch.setattr(service, "_jobs", {})
    monkeypatch.setattr(service, "_job_logs", {})
    monkeypatch.setattr(service, "_active_jobs", set())
    monkeypatch.setattr(retention, "start_scheduler", lambda *a: None)
    monkeypatch.setattr(retention, "run_in_background", lambda *a: None)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    class ParkedThread:
        def __init__(self, **kwargs): pass
        def start(self): pass
    monkeypatch.setattr(service.threading, "Thread", ParkedThread)
    c = service.app.test_client()
    with c.session_transaction() as session:
        session.update(authenticated=True, cancel_csrf="fixture-csrf")
    return c


def start(client, card):
    data = {"manuscript_text": "ハブの案件から始めた図解。" * 20, "provider": "gpt-image", "target_count": "5", "card": card}
    response = client.post("/start", data=data)
    assert response.status_code == 200
    return response.json["job_id"]


def test_index_keeps_only_a_valid_card(client):
    page = client.get("/?card=Ab3dE6gH&channel=russia").get_data(as_text=True)
    assert 'name="card" id="hubCard" value="Ab3dE6gH"' in page
    assert "案件（Trello のカード" in page
    bad = client.get('/?card="><script>').get_data(as_text=True)
    assert 'id="hubCard" value=""' in bad
    assert "<script>\"" not in bad


def test_start_saves_card_and_restart_carries_it(client, tmp_path):
    job_id = start(client, "Ab3dE6gH")
    assert load_json(tmp_path / job_id / "request.json", {})["card"] == "Ab3dE6gH"
    assert client.get(f"/api/restart-settings/{job_id}").json["settings"]["card"] == "Ab3dE6gH"
    other = start(client, "../../etc")
    assert load_json(tmp_path / other / "request.json", {})["card"] == ""


def test_job_thread_sets_the_card_for_ai_calls(client, tmp_path, monkeypatch):
    job_id = start(client, "Ab3dE6gH")
    seen = []
    monkeypatch.setattr(runtime, "set_card", seen.append)
    service._set_job_card(job_id)
    service._set_job_card("missing-job")
    assert seen == ["Ab3dE6gH", ""]


def test_hub_source_reads_only_russia_cards_through_the_worker(client):
    import trello_source
    doc = {"title": "原稿", "text": "本文", "content_kind": "manuscript", "provider": "Googleドキュメント",
           "choices": [{"label": "原稿", "url": "https://docs.google.com/document/d/abc/edit"}]}
    with patch("cloudflare_access.verified_email", return_value="member@example.test"), \
         patch.object(trello_source, "_request", return_value=doc) as read:
        data = client.get("/api/trello-source?card=Ab3dE6gH").get_json()
        assert data["ok"] and data["text"] == "本文"
        assert read.call_args.args == ("roshia", "member@example.test")
        assert read.call_args.kwargs["source_id"] == "trello:Ab3dE6gH"
        bad = client.get("/api/trello-source?card=../x").get_json()
        assert not bad["ok"] and read.call_count == 1  # 正しくないカードは中継へ送らない
    with patch("cloudflare_access.verified_email", return_value=""), patch.object(trello_source, "_request") as read:
        assert not client.get("/api/trello-source?card=Ab3dE6gH").get_json()["ok"]
        read.assert_not_called()  # 共通ログインのメールが無ければ読まない


def test_index_shows_the_hub_source_panel_only_with_a_card(client):
    assert 'id="hubSource" class="text-sm' in client.get("/?card=Ab3dE6gH").get_data(as_text=True)
    assert 'id="hubSource" class="hidden ' in client.get("/").get_data(as_text=True)
