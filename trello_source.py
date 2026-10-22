"""制作の進行ハブから開いた案件の Trello カードから、原稿の本文を PC のワーカー経由で読む（2026-10-22）。

素材スタジオ（keizai-sentence-tool/trello_source.py）と同じ中継・同じ本人確認（共通ログインのメールと組織図の担当）。
LLM は呼ばない。本文は画面の原稿欄に入れるだけで、生成は人が「図解を一気に生成する」を押した時に始まる。
"""
from __future__ import annotations

import hmac
import re
import time
import uuid

# 図解スタジオはロシア解体新書だけ（組織図の番号 → ワーカーの資料読み込みのチャンネル）
WORKER_CHANNELS = {"russia": "roshia"}
DOC = re.compile(r"https://docs\.google\.com/document/d/[A-Za-z0-9_-]+")


class SourceError(ValueError):
    pass


def _request(channel: str, email: str, *, source_id: str, resource: str = "") -> dict:
    import subscription_runtime as transport
    ident = "zukai-source-" + uuid.uuid4().hex
    conf = transport._gateway_conf()
    if not conf:
        raise SourceError("原稿の読み込みサービスに接続できません。原稿は貼り付けでも入れられます。")
    message = "\0".join(("thumbnail-source-v1", ident, channel, email.strip().casefold()))
    proof = hmac.new(conf[1].encode(), message.encode(), "sha256").hexdigest()
    payload = {"id": ident, "tool": "zukai", "kind": "thumbnail_sources", "operation": "read",
               "channel": channel, "requestor_proof": proof, "source_id": source_id,
               "resource": resource, "created": time.time()}
    try:
        code, heartbeat = transport._storage("GET", "hb/worker.json")
        if (code != 200 or time.time() - float(heartbeat.get("ts", 0)) > 60
                or "thumbnail_sources_v1" not in heartbeat.get("capabilities", [])):
            raise SourceError("原稿の読み込みサービスに接続できません。原稿は貼り付けでも入れられます。")
        code, _ = transport._storage("POST", f"req/{ident}.json", payload)
        if code not in (200, 201):
            raise SourceError("原稿の読み込みを受け付けられませんでした。少し待ってもう一度お試しください。")
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            time.sleep(2)
            code, result = transport._storage("GET", f"res/{ident}.json")
            if code != 200:
                continue
            if result.get("id") != ident or result.get("kind") != "thumbnail_sources":
                raise SourceError("原稿の応答を確認できませんでした。")
            if not result.get("ok"):
                raise SourceError(result.get("error") or "原稿を読み取れませんでした。")
            return result["data"]
        raise SourceError("原稿の読み込みの待ち時間を超えました。少し待ってもう一度お試しください。")
    except SourceError:
        raise
    except Exception:
        raise SourceError("原稿の読み込みに接続できませんでした。原稿は貼り付けでも入れられます。") from None


def load(channel_id: str, email: str, card_url: str, resource: str = "") -> dict:
    """カードを読み、原稿（Googleドキュメント）が1つだけならその本文まで読む。複数なら選べる一覧を返す。"""
    if not email:
        raise SourceError("ハブの案件の原稿は、共通ログイン（zukai.apprendre.jp）から開いた時に読み込めます。")
    channel = WORKER_CHANNELS.get(channel_id)
    match = re.fullmatch(r"https://trello\.com/c/([A-Za-z0-9]{8})", str(card_url or ""))
    if not channel or not match:
        raise SourceError("この案件は Trello のカードとつながっていません。原稿を貼り付けてください。")
    source_id = "trello:" + match[1]
    data = _request(channel, email, source_id=source_id, resource=resource)
    if not resource and data.get("content_kind") != "manuscript":
        docs = [c for c in data.get("choices") or [] if DOC.match(str(c.get("url", "")))]
        if len(docs) == 1:
            data = _request(channel, email, source_id=source_id, resource=docs[0]["url"])
    return {"title": str(data.get("title") or ""), "text": str(data.get("text") or ""),
            "provider": str(data.get("provider") or ""), "status": str(data.get("status") or ""),
            "manuscript": data.get("content_kind") == "manuscript",
            "choices": [{"label": str(c.get("label") or "原稿・資料")[:80], "url": str(c.get("url"))}
                        for c in data.get("choices") or [] if DOC.match(str(c.get("url", "")))]}
