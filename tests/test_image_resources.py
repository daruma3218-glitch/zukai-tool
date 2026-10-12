"""No paid calls: global limits, cancellation, resource cleanup, unchanged pixels."""
from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as appmod
import generator
import image_resources
import map_renderer
from job_control import JobCancelled


def test_global_limit_covers_multiple_jobs_and_maps(tmp_path, monkeypatch):
    """The same gate applies to separate providers and the map path."""
    active = peak = 0
    lock = threading.Lock()

    def work(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.035)
        with lock:
            active -= 1

    def api(**kwargs):
        work()
        return SimpleNamespace(data=[SimpleNamespace(b64_json="eA==", url=None)])

    def map_draw(*args, **kwargs):
        work()
        return Image.new("RGB", (16, 9)), {}

    monkeypatch.setattr(generator, "_save_as_16_9", lambda *a: None)
    monkeypatch.setattr(map_renderer, "render_map", map_draw)
    client = SimpleNamespace(images=SimpleNamespace(generate=api, edit=api))
    tasks = [lambda i=i: generator._sync_generate_image_openai(client, "test", tmp_path/f"{i}.png")
             for i in range(8)]
    tasks += [lambda i=i: map_renderer.render_map_file({}, tmp_path/f"map{i}.png") for i in range(6)]
    with ThreadPoolExecutor(max_workers=14) as pool:
        results = list(pool.map(lambda f: f(), tasks))
    assert all(r[0] for r in results)
    assert peak == image_resources.IMAGE_TASK_LIMIT
    assert active == 0


def test_cancelled_waiter_never_calls_api_and_releases_slots():
    entered = threading.Event()
    cancel = threading.Event()
    called = []

    def check():
        entered.set()
        if cancel.is_set():
            raise JobCancelled()

    @image_resources.limited_image_task
    def run(cancel_check=None):
        called.append(True)

    for _ in range(image_resources.IMAGE_TASK_LIMIT):
        image_resources._slots.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run, check)  # Also checks a positional cancellation callback.
            assert entered.wait(2)
            cancel.set()
            with pytest.raises(JobCancelled):
                future.result(timeout=2)
    finally:
        for _ in range(image_resources.IMAGE_TASK_LIMIT):
            image_resources._slots.release()
    assert not called
    run()
    assert called == [True]


def test_failure_does_not_consume_a_slot():
    @image_resources.limited_image_task
    def fail():
        raise ValueError("expected")
    for _ in range(image_resources.IMAGE_TASK_LIMIT + 2):
        with pytest.raises(ValueError, match="expected"):
            fail()


@pytest.mark.parametrize("mode,size", [("RGB", (160, 90)), ("RGBA", (120, 100)), ("L", (100, 40))])
def test_padding_preserves_pixels_and_closes_images(tmp_path, monkeypatch, mode, size):
    with Image.new(mode, size, 128) as source, io.BytesIO() as buf:
        source.save(buf, format="PNG")
        data = buf.getvalue()
    opened = []
    real_open = Image.open
    def capture(*args, **kwargs):
        image = real_open(*args, **kwargs)
        opened.append(image)
        return image
    monkeypatch.setattr(Image, "open", capture)
    output = tmp_path / "out.png"
    generator._save_as_16_9(data, output)
    for image in opened:
        with pytest.raises(ValueError):
            image.getpixel((0, 0))
    with real_open(output) as image:
        expected = size if abs(size[0]/size[1] - 16/9) < .01 else (
            (size[0], round(size[0]/(16/9))) if size[0]/size[1] > 16/9 else (round(size[1]*16/9), size[1]))
        assert image.size == expected
        if mode != "RGBA":
            pixel = image.getpixel((image.width//2, image.height//2))
            assert pixel == ((128, 128, 128) if mode == "L" else (128, 0, 0))


def test_save_failure_closes_source_and_canvas(tmp_path, monkeypatch):
    with Image.new("RGB", (120, 100)) as source, io.BytesIO() as buf:
        source.save(buf, format="PNG")
        data = buf.getvalue()
    held = []
    def fail(image, path):
        held.append(image)
        raise OSError("disk full")
    monkeypatch.setattr(generator, "_save_png", fail)
    with pytest.raises(OSError, match="disk full"):
        generator._save_as_16_9(data, tmp_path/"out.png")
    with pytest.raises(ValueError):
        held[0].getpixel((0, 0))


def test_map_file_releases_returned_image_on_failure(tmp_path, monkeypatch):
    image = Image.new("RGB", (20, 10))
    monkeypatch.setattr(map_renderer, "render_map", lambda *a, **k: (image, {}))
    with mock.patch.object(image, "save", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            map_renderer.render_map_file({}, tmp_path/"map.png")
    with pytest.raises(ValueError):
        image.getpixel((0, 0))


def test_form_and_server_clamp_stale_concurrency(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder-no-network")
    monkeypatch.setattr(appmod, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "test-password")
    monkeypatch.setattr(appmod, "_jobs", {})
    monkeypatch.setattr(appmod, "_job_logs", {})
    monkeypatch.setattr(appmod, "_active_jobs", set())
    monkeypatch.setattr(appmod.threading, "Thread", mock.Mock())
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
    html = client.get("/").get_data(as_text=True)
    assert f'max="{image_resources.IMAGE_TASK_LIMIT}"' in html
    assert client.get("/version").json["memory_safety"]["image_task_limit"] == image_resources.IMAGE_TASK_LIMIT
    response = client.post("/start", data={"manuscript_text": "原稿"*300, "concurrency": "24",
                                          "provider": "gpt-image"})
    assert response.status_code == 200
    assert next(iter(appmod._jobs.values()))["concurrency"] == image_resources.IMAGE_TASK_LIMIT
