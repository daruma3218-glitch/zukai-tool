"""Cloudflare Access のログインの証明を確かめる（2026-10-14・原稿スタジオ・全体管理基盤と同じ方式）。

新しい入口（〇〇.apprendre.jp）は Cloudflare Access（チーム apprendre・登録メールへのコード）の後ろにある。
Access を通った要求には、署名つきの証明（ヘッダー Cf-Access-Jwt-Assertion）が付く。
ここでは署名（RS256・チームの公開鍵）・発行元・宛先（このアプリの AUD）・期限をすべて確かめ、
合えばログイン済みとして扱う。どれか1つでも合わなければ「証明なし」と同じ扱い（今までのパスワードの画面）。

- 設定（環境変数 CF_ACCESS_AUD）が無ければ何もしない＝今までのパスワードのログインだけ。
- 今までの住所（onrender.com など）とパスワードは、並行の期間はそのまま使える。
- 作られた証明（ヘッダーを手で付けた要求）は、署名が合わないので通らない。
- 証明の中身・鍵・メールはログに出さない（合わなかった理由の種類だけ）。
"""
from __future__ import annotations

import logging
import os
import threading

import jwt

HEADER = "Cf-Access-Jwt-Assertion"
DEFAULT_TEAM = "https://apprendre.cloudflareaccess.com"

_log = logging.getLogger(__name__)
_LOCK = threading.Lock()
_CLIENT: tuple | None = None


def _settings() -> tuple[str, tuple[str, ...]]:
    aud = tuple(a.strip() for a in os.environ.get("CF_ACCESS_AUD", "").split(",") if a.strip())
    team = (os.environ.get("CF_ACCESS_TEAM_DOMAIN", "") or DEFAULT_TEAM).strip().rstrip("/")
    return team, aud


def enabled() -> bool:
    """Cloudflare のログインを受け付ける設定か（AUD が入っているか）。"""
    return bool(_settings()[1])


def _jwks(team: str) -> "jwt.PyJWKClient":
    global _CLIENT
    with _LOCK:
        if _CLIENT is None or _CLIENT[0] != team:
            # 公開鍵は1時間覚える（Cloudflare は約6週間ごとに入れ替え、前の鍵も7日は有効）
            _CLIENT = (team, jwt.PyJWKClient(team + "/cdn-cgi/access/certs", cache_keys=True, lifespan=3600, timeout=10))
        return _CLIENT[1]


def verified_email(headers, *, key_resolver=None) -> str:
    """証明が正しければメール（小文字）を返す。無い・合わない時は空の文字。例外は外に出さない。
    key_resolver は試験用（token → 公開鍵）。"""
    team, aud = _settings()
    try:
        token = (headers.get(HEADER) or "").strip()
    except Exception:
        return ""
    if not aud or not token:
        return ""
    try:
        if jwt.get_unverified_header(token).get("alg") != "RS256":
            return ""
        key = key_resolver(token) if key_resolver else _jwks(team).get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=list(aud), issuer=team, leeway=30,
                            options={"require": ["exp", "iat", "iss", "aud"]})
    except Exception as exc:  # 署名違い・期限切れ・宛先違い・鍵の取得の失敗など
        _log.warning("cloudflare access: proof rejected (%s)", type(exc).__name__)
        return ""
    email = str(claims.get("email") or "").strip().lower()
    return email if "@" in email else ""
