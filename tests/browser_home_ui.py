"""Chrome entry/history check with synthetic records. No production API or provider."""
import json
import logging
import sys
import threading
from pathlib import Path

from flask import Flask, render_template, request
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
app = Flask(__name__, template_folder=str(ROOT / "templates"), static_folder=str(ROOT / "static"))


@app.get("/")
def home():
    rows = [dict(id=f"20261110_12000{i}", title=f"確認用 {i}｜暮らしと価格のしくみ", date="20261110", status=status,
                 succeeded=2 if status == "completed" else 1, target=2)
            for i, status in enumerate(["completed", "running", "interrupted", "queued", "cancelled", "completed"])]
    return render_template("upload.html", hub_card="", past_jobs=[] if request.args.get("empty") else rows,
                           last_run=None, image_task_limit=2, has_anthropic=False, has_gemini=True, has_openai=True,
                           openai_image_models=[dict(id="gpt-image-2.6", label="GPT Image 2.6")],
                           default_openai_model="gpt-image-2.6", retention_notice="")


def main(output):
    output.mkdir(parents=True, exist_ok=True)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    results = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            try:
                for width in [390, 1366]:
                    context = browser.new_context(viewport={"width":width, "height":900})
                    page = context.new_page()
                    errors, writes = [], []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("request", lambda req: writes.append(req.url) if req.method != "GET" else None)
                    page.goto(origin)
                    expect(page.locator("#submitBtn")).to_be_disabled()
                    rows = page.locator("[data-history-job]:visible")
                    expect(rows).to_have_count(6)
                    page.locator('[data-history-filter="attention"]').click()
                    expect(rows).to_have_count(2)
                    page.locator("#history-search").fill("価格　２｜")
                    expect(rows).to_have_count(1)
                    page.reload()
                    expect(rows).to_have_count(1)
                    expect(page.locator("#history-search")).to_have_value("価格　２｜")
                    page.locator("#history-search").fill("見つからない語")
                    expect(page.locator("#history-empty")).to_be_visible()
                    page.locator("#history-reset").click()
                    expect(rows).to_have_count(6)
                    page.locator('[data-history-filter="working"]').click()
                    expect(rows).to_have_count(2)
                    page.locator("#history-clear").click()
                    expect(rows).to_have_count(6)
                    page.locator("#manuscriptText").fill("画面の確認に使う架空の原稿です。" * 15)
                    expect(page.locator("#submitBtn")).to_be_enabled()
                    page.locator('a[href="#studio-history"]').click()
                    expect(page.locator("#history-search")).to_be_in_viewport()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    page.evaluate("scrollTo(0,0); document.activeElement.blur()")
                    page.screenshot(path=str(output / f"zukai-home-{width}.png"), full_page=True)
                    page.goto(origin + "/?empty=1")
                    expect(page.get_by_text("最初の図解をつくりましょう")).to_be_visible()
                    assert not errors and not writes, (errors, writes)
                    results.append(dict(width=width, history=6, writes=0, errors=0, checks="search/fullwidth/filter/restore/reset/input/jump/empty/overflow"))
                    context.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    (output / "zukai-home-checks.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
