#!/usr/bin/env python3
"""ディレクター向けの追加API（手直し・採用・採用分ZIP）。app.py から register() で登録する。

app.py への変更を小さく保ち、既存の生成・ダウンロードの動きには触れない。
"""

import re
from pathlib import Path

from flask import jsonify, request, send_file

import image_edit
from utils import load_json

JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def register(app, login_required, output_dir_getter, items_getter):
    """output_dir_getter は現在の出力先を返す関数（テストで差し替えられるよう毎回呼ぶ）。
    items_getter(job_dir) は画面と同じ画像一覧（_image_snapshot の items）を返す関数。"""

    def job_dir_for(job_id: str) -> Path:
        if not JOB_NAME_RE.match(job_id or ""):
            raise image_edit.EditError("ジョブの指定が不正です")
        path = Path(output_dir_getter()) / job_id
        if not path.is_dir():
            raise FileNotFoundError(job_id)
        return path

    def fail(exc):
        if isinstance(exc, FileNotFoundError):
            return jsonify({"error": "ジョブが見つかりません"}), 404
        if isinstance(exc, TimeoutError):
            return jsonify({"error": str(exc)}), 503
        return jsonify({"error": str(exc)}), 400

    @app.route("/api/edit/<job_id>", methods=["POST"], endpoint="director_edit")
    @login_required
    def director_edit(job_id):
        body = request.get_json(silent=True) or {}
        try:
            entry = image_edit.request_edit(job_dir_for(job_id), body.get("source", ""),
                                            body.get("action", ""), body.get("instruction", ""))
        except (image_edit.EditError, FileNotFoundError, TimeoutError) as exc:
            return fail(exc)
        return jsonify({"edit": entry, "edits": image_edit.load_edits(job_dir_for(job_id))})

    @app.route("/api/edits/<job_id>", endpoint="director_edits")
    @login_required
    def director_edits(job_id):
        try:
            job_dir = job_dir_for(job_id)
        except (image_edit.EditError, FileNotFoundError) as exc:
            return fail(exc)
        return jsonify({"edits": image_edit.load_edits(job_dir),
                        "adopted": sorted(image_edit.load_adoption(job_dir))})

    @app.route("/api/adopt/<job_id>", methods=["POST"], endpoint="director_adopt")
    @login_required
    def director_adopt(job_id):
        body = request.get_json(silent=True) or {}
        try:
            table = image_edit.set_adopted(job_dir_for(job_id), body.get("filename", ""),
                                           bool(body.get("adopted", True)), body.get("group"))
        except (image_edit.EditError, FileNotFoundError, TimeoutError) as exc:
            return fail(exc)
        return jsonify({"adopted": sorted(table)})

    @app.route("/download-adopted/<job_id>", endpoint="director_download_adopted")
    @login_required
    def director_download_adopted(job_id):
        try:
            job_dir = job_dir_for(job_id)
        except (image_edit.EditError, FileNotFoundError):
            return "結果が見つかりません", 404
        manifest = load_json(job_dir / "manifest.json", {})
        title = manifest.get("title") or load_json(job_dir / "job.json", {}).get("title") or job_id
        try:
            start = min(max(int(request.args.get("start", "1")), 1), 99999)
        except ValueError:
            start = 1
        archive, count = image_edit.adopted_zip(job_dir, items_getter(job_dir), title, start=start)
        if not count:
            archive.close()
            return "採用した画像がまだありません。画像の「採用」を押してから取り出してください。", 409
        safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:50] or job_id
        size = archive.seek(0, 2)
        archive.seek(0)
        response = send_file(archive, mimetype="application/zip", as_attachment=True,
                             download_name=f"{safe_title}_{job_id}_採用{count}枚.zip")
        response.content_length = size
        response.headers["Cache-Control"] = "no-store"
        response.call_on_close(archive.close)
        return response
