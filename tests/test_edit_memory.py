"""Real SDK + mock HTTP, no paid requests: response lifetime and edit memory."""
import base64
from concurrent.futures import ThreadPoolExecutor
import gc
import hashlib
import io
import json
from pathlib import Path
import sys
import threading
import weakref

import httpx
import openai
from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generator
import image_resources


def png():
    with io.BytesIO() as buffer, Image.new("RGB", (96, 54), "green") as image:
        image.save(buffer, "PNG")
        return buffer.getvalue()


class Body(httpx.SyncByteStream):
    def __init__(self, data, on_read=lambda: None):
        self.data, self.on_read, self.closed = data, on_read, False

    def __iter__(self):
        self.on_read()
        yield self.data

    def close(self):
        self.closed = True


def offline_client(handler):
    return openai.OpenAI(api_key="offline-test", base_url="https://offline.invalid/v1",
                         max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("failure", ["no_image", "decode", "write", "rate", "timeout"])
def test_failed_attempt_closes_body_and_collects_before_retry(tmp_path, monkeypatch, failure):
    source, output = tmp_path / "source.png", tmp_path / "edited.png"
    source.write_bytes(png())
    adoption = tmp_path / "adoption.json"
    adoption.write_text('{"adopted":{"source.png":{}}}')
    original = {p: p.read_bytes() for p in (source, adoption)}
    streams, requests, pauses = [], [], []
    save = generator._save_png

    def respond(request):
        assert request.url.host == "offline.invalid" and request.url.path == "/v1/images/edits"
        requests.append(request)
        if len(requests) == 1 and failure == "timeout":
            raise httpx.ReadTimeout("offline timeout", request=request)
        payload = {"data": [{"b64_json": base64.b64encode(png()).decode()}]}
        status = 200
        if len(requests) == 1:
            if failure == "no_image":
                payload = {"data": []}
            elif failure == "decode":
                payload = {"data": [{"b64_json": "eA=="}]}
            elif failure == "rate":
                payload, status = {"error": {"message": "offline rate limit", "type": "rate_limit"}}, 429
        body = Body(json.dumps(payload).encode())
        streams.append(body)
        return httpx.Response(status, headers={"content-type": "application/json"}, stream=body)

    writes = []
    def write(image, path):
        writes.append(True)
        if failure == "write" and len(writes) == 1:
            raise OSError("synthetic disk full")
        save(image, path)

    def pause(delay):
        assert all(s.closed for s in streams), "retry must not retain an open response"
        pauses.append(delay)

    monkeypatch.setattr(generator, "_save_png", write)
    monkeypatch.setattr(generator.time, "sleep", pause)
    with offline_client(respond) as client:
        ok, error = generator._sync_edit_image_openai(client, source, "test", output)
    assert ok, error
    assert len(requests) == 2 and all(s.closed for s in streams)
    assert bool(pauses) == (failure != "no_image")
    for path, contents in original.items():
        assert path.read_bytes() == contents
    with Image.open(output) as image:
        assert image.size == (96, 54)


def test_completed_sdk_responses_are_not_left_for_automatic_gc(tmp_path):
    """Explicit cleanup must work even between Python's GC thresholds."""
    source = tmp_path / "source.png"
    source.write_bytes(png())
    responses = []
    def respond(request):
        response = httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png()).decode()}]})
        responses.append(weakref.ref(response))
        return response
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        with offline_client(respond) as client:
            for i in range(8):
                assert generator._sync_edit_image_openai(client, source, "test", tmp_path / f"out{i}.png")[0]
                assert all(ref() is None for ref in responses)
    finally:
        if was_enabled:
            gc.enable()


def test_two_remote_edits_read_large_bodies_one_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(image_resources, "_slots", threading.BoundedSemaphore(2))
    monkeypatch.setattr(image_resources, "_edit_pixels", threading.BoundedSemaphore(1))
    source = tmp_path / "source.png"
    source.write_bytes(png())
    before = hashlib.sha256(source.read_bytes()).digest()
    first_request, second_request = threading.Event(), threading.Event()
    first_read, second_read, release = threading.Event(), threading.Event(), threading.Event()
    payload = json.dumps({"data": [{"b64_json": base64.b64encode(png()).decode()}]}).encode()
    streams = []

    def first(request):
        first_request.set()
        assert second_request.wait(5), "remote HTTP work should remain parallel"
        def consume():
            first_read.set()
            assert release.wait(5)
        body = Body(payload, consume)
        streams.append(body)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=body)

    def second(request):
        second_request.set()
        assert first_read.wait(5)
        body = Body(payload, second_read.set)
        streams.append(body)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=body)

    with offline_client(first) as a, offline_client(second) as b, ThreadPoolExecutor(2) as pool:
        one = pool.submit(generator._sync_edit_image_openai, a, source, "test", tmp_path / "a.png")
        try:
            assert first_request.wait(5)
            two = pool.submit(generator._sync_edit_image_openai, b, source, "test", tmp_path / "b.png")
            assert second_request.wait(5) and first_read.wait(5)
            assert not second_read.wait(0.1), "second body must stay unread until pixel buffers are released"
        finally:
            release.set()
        assert one.result(10)[0] and two.result(10)[0]
    assert second_read.is_set() and all(s.closed for s in streams)
    assert hashlib.sha256(source.read_bytes()).digest() == before


def test_cleanup_failure_cannot_leak_request_slot(monkeypatch):
    monkeypatch.setattr(image_resources, "_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(image_resources, "release_unused_memory", lambda: (_ for _ in ()).throw(OSError("test")))
    with pytest.raises(OSError):
        with image_resources.image_task():
            pass
    assert image_resources._slots.acquire(blocking=False)
    image_resources._slots.release()
