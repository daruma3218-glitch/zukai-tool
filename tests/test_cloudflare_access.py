"""旧入口・過去のセッションを拒否し、新入口と機械の診断口を維持する。"""
import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as appmod
import cloudflare_access as ca

TEAM = "https://apprendre.cloudflareaccess.com"
AUD = "aud-for-studios"
ORIGIN = "https://zukai-studio.onrender.com"
CANONICAL = "https://zukai.apprendre.jp"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(aud=AUD, iss=TEAM, exp=None, key=KEY, email="someone@example.com"):
    now = int(time.time())
    return jwt.encode({"email": email, "aud": [aud], "iss": iss, "iat": now,
                       "exp": exp if exp is not None else now + 600}, key, algorithm="RS256")


class _Keys:
    def get_signing_key_from_jwt(self, _token):
        return type("K", (), {"key": KEY.public_key()})()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    monkeypatch.setattr(ca, "_jwks", lambda team: _Keys())
    monkeypatch.setattr(appmod, "APP_PASSWORD", "test-password")
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def test_valid_proof_is_required_on_each_request(client):
    headers = {ca.HEADER: token()}
    assert client.get("/", base_url=CANONICAL, headers=headers).status_code == 200
    assert client.get("/api/key-attribution", base_url=CANONICAL, headers=headers).status_code == 200
    assert client.get("/api/key-attribution", base_url=CANONICAL).status_code == 401


@pytest.mark.parametrize("bad", [lambda: token(aud="another-app"),
    lambda: token(iss="https://other.cloudflareaccess.com"),
    lambda: token(exp=int(time.time()) - 3600), lambda: token(key=OTHER),
    lambda: token(email=""), lambda: "forged"])
def test_invalid_proof_cannot_reuse_an_old_session(client, bad):
    with client.session_transaction(base_url=CANONICAL) as session:
        session["authenticated"] = True
    response = client.get("/api/key-attribution", base_url=CANONICAL, headers={ca.HEADER: bad()})
    assert response.status_code == 401 and response.is_json


def test_old_password_and_session_no_longer_grant_access(client):
    assert client.post("/login", base_url=ORIGIN, data={"password": "test-password"}).status_code == 401
    with client.session_transaction(base_url=ORIGIN) as session:
        session["authenticated"] = True
    assert client.get("/api/key-attribution", base_url=ORIGIN).status_code == 401
    assert client.post("/start", base_url=ORIGIN).status_code == 401


def test_old_links_preserve_path_and_query_but_go_to_the_protected_host(client):
    response = client.get("/progress/example?channel=otona", base_url=ORIGIN)
    assert response.status_code == 302
    assert response.headers["Location"] == CANONICAL + "/progress/example?channel=otona"
    assert response.headers["Cache-Control"] == "no-store"
    assert client.get("/login", base_url=ORIGIN).headers["Location"] == CANONICAL


def test_api_is_denied_instead_of_redirecting_even_with_a_forged_host(client):
    response = client.get("/api/key-attribution", base_url=CANONICAL)
    assert response.status_code == 401 and response.is_json


def test_non_secret_read_only_version_stays_available(client):
    response = client.get("/version", base_url=ORIGIN)
    assert response.status_code == 200 and response.is_json
    assert client.post("/version", base_url=ORIGIN).status_code == 401


def test_production_missing_audience_fails_closed_even_without_password(client, monkeypatch):
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-offline-test")
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    monkeypatch.setattr(appmod, "APP_PASSWORD", "")
    assert client.get("/", base_url=CANONICAL, headers={ca.HEADER: token()}).status_code == 401
    assert client.post("/login", base_url=ORIGIN, data={"password": ""}).status_code == 401


def test_local_development_without_cloudflare_keeps_password_login(client, monkeypatch):
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    response = client.get("/")
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    assert client.post("/login", data={"password": "test-password"}).status_code == 302
    assert client.get("/").status_code == 200
