#!/usr/bin/env python3
"""サブスクLLMゲートウェイ (zukai-tool Render側・2026-08-13)

目的:
    Render 上で走る図解つくーる (ロシア解体新書用) の Claude 呼び出しを、社長PCの常駐ワーカー
    (subsk-worker) へ転送し、Claude Code CLI = サブスク枠 (API課金ゼロ) で
    処理してもらう。ワーカーが不在なら即座に None を返し、呼び出し側は
    従来どおり Anthropic API 直呼び (課金) で処理する。

設計原則 (スタッフのUXを変えない):
    - 画面・URL・ジョブフロー・出力物は一切変更しない。差し替わるのは
      claude_research / claude_query 内の「テキスト生成の転送路」だけ。
    - ワーカー不在 (社長PC停止・ハートビート停止) → 数秒で API へフォールバック。
      ツールが止まることはない。
    - SUPABASE_URL / SUPABASE_KEY が未設定なら完全に無効 (従来動作のまま)。
      = Render の環境変数を設定するまでこのファイルは何もしない。

転送路: Supabase Storage (バケット subsk-gateway・非公開)
    hb/worker.json   ワーカーのハートビート (15秒毎更新)
    req/<id>.json    リクエスト {id, tool, kind, model, system, query, max_tokens, max_uses}
    res/<id>.json    レスポンス {id, ok, text | error, engine, elapsed}
    テーブル不要 (REST のみ) なので Supabase 側のセットアップ作業ゼロ。

環境変数:
    SUPABASE_URL / SUPABASE_KEY  … 転送路 (otona-manabi-tv と同じ Supabase)
    SUBSK_GW=0                   … 明示的に無効化 (ロールバック用)
    SUBSK_GW_TIMEOUT             … ワーカー応答待ちの上限秒 (既定 420)
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid

BUCKET = "subsk-gateway"
HEARTBEAT_FRESH_SEC = 60      # ハートビートがこれより古ければワーカー不在とみなす
POLL_INTERVAL_SEC = 2.5
DEFAULT_TIMEOUT_SEC = int(os.environ.get("SUBSK_GW_TIMEOUT", "420"))


def _conf() -> tuple[str, str] | None:
    if os.environ.get("SUBSK_GW", "1").strip() == "0":
        return None
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        return None
    return url, key


def _storage(method: str, path: str, body=None, headers=None, timeout: float = 20.0):
    """Storage REST 呼び出し。(status, bytes) を返す。失敗は HTTPError の status。"""
    conf = _conf()
    if not conf:
        raise RuntimeError("gateway disabled")
    url, key = conf
    h = {"Authorization": f"Bearer {key}", "apikey": key}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(f"{url}{path}", data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _worker_alive() -> bool:
    try:
        status, body = _storage("GET", f"/storage/v1/object/{BUCKET}/hb/worker.json", timeout=10.0)
        if status != 200:
            return False
        hb = json.loads(body.decode("utf-8"))
        return (time.time() - float(hb.get("ts", 0))) <= HEARTBEAT_FRESH_SEC
    except Exception:
        return False


WORKER_RETRY_WINDOW_SEC = 30   # 不在に見えても復帰をこの秒数まで待つ (ハートビート15秒毎×2周+余裕)
WORKER_RETRY_POLL_SEC = 7.5


def _worker_alive_with_retry() -> bool:
    """ワーカー不在に見えても最大 WORKER_RETRY_WINDOW_SEC 秒は復帰を待ってから判定する。

    ワーカーのハートビートはポーリング処理のネットワーク遅延で一時的に途切れる
    ことがあり (2026-08-25/27 の API 課金の真因)、即断すると稼働中なのに
    API 直呼び (課金) へ落ちる。本当に不在 (社長PC停止等) なら30秒余計に
    待つだけで、従来どおり API へフォールバックする。
    """
    if _worker_alive():
        return True
    deadline = time.time() + WORKER_RETRY_WINDOW_SEC
    while time.time() < deadline:
        time.sleep(WORKER_RETRY_POLL_SEC)
        if _worker_alive():
            print("[subsk-gw] ワーカー一時不応答 → 復帰確認 (API直呼び回避)", flush=True)
            return True
    return False


def gateway_generate(
    kind: str,
    system: str,
    query: str,
    max_tokens: int,
    max_uses: int = 0,
    model: str = "claude-sonnet-5",
    tool: str = "zukai",
) -> str | None:
    """ワーカー経由 (サブスク枠) でテキスト生成。

    Returns:
        str  … 生成テキスト (API課金なし)
        None … ワーカー不在/無効/タイムアウト。呼び出し側は従来の API 直呼びへ。
    """
    if _conf() is None:
        return None
    try:
        if not _worker_alive_with_retry():
            print(f"[subsk-gw] ワーカー不在 ({WORKER_RETRY_WINDOW_SEC}秒再確認しても応答なし) → API直呼び(課金)", flush=True)
            return None

        req_id = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
        payload = {
            "id": req_id,
            "tool": tool,
            "kind": kind,                 # "research" (web検索あり) | "query" (なし)
            "model": model,
            "system": system,
            "query": query,
            "max_tokens": max_tokens,
            "max_uses": max_uses,
            "channel": "russia",  # 図解つくーるはロシア解体新書専用 (2026-08-13 社長確認)
            "created": time.time(),
        }
        status, body = _storage(
            "POST", f"/storage/v1/object/{BUCKET}/req/{req_id}.json",
            payload, headers={"x-upsert": "true"},
        )
        if status != 200:
            print(f"[subsk-gw] リクエスト投函失敗 ({status}) → API直呼び(課金)", flush=True)
            return None

        deadline = time.time() + DEFAULT_TIMEOUT_SEC
        res_path = f"/storage/v1/object/{BUCKET}/res/{req_id}.json"
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL_SEC)
            status, body = _storage("GET", res_path, timeout=15.0)
            if status != 200:
                continue
            try:
                res = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            # 後片付け (best effort)
            try:
                _storage("DELETE", res_path, timeout=10.0)
            except Exception:
                pass
            if res.get("ok") and res.get("text"):
                print(
                    f"[subsk-gw] ワーカー処理完了 ({len(res['text'])}字, "
                    f"{res.get('elapsed', '?')}秒, API課金なし)", flush=True,
                )
                return res["text"]
            print(f"[subsk-gw] ワーカー側エラー → API直呼び(課金): {str(res.get('error'))[:150]}", flush=True)
            return None

        # タイムアウト: リクエストを取り下げてからフォールバック (ゾンビ処理防止)
        try:
            _storage("DELETE", f"/storage/v1/object/{BUCKET}/req/{req_id}.json", timeout=10.0)
        except Exception:
            pass
        print(f"[subsk-gw] {DEFAULT_TIMEOUT_SEC}秒待っても応答なし → API直呼び(課金)", flush=True)
        return None
    except Exception as e:
        print(f"[subsk-gw] 例外 → API直呼び(課金): {type(e).__name__}: {str(e)[:120]}", flush=True)
        return None
