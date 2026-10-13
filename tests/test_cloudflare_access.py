"""Cloudflare のログイン（新しい入口）と今までのパスワード（並行の期間）の両方で入れること（2026-10-14）。外部APIなし。"""
import time
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

import app as appmod
import cloudflare_access as ca

TEAM = "https://apprendre.cloudflareaccess.com"
AUD = "aud-for-studios"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(aud=AUD, iss=TEAM, exp=None, key=KEY, email="someone@example.com"):
    now = int(time.time())
    return jwt.encode({"email": email, "aud": [aud], "iss": iss, "iat": now, "exp": exp if exp is not None else now + 600},
                      key, algorithm="RS256")


class _Keys:
    def get_signing_key_from_jwt(self, _token):
        return type("K", (), {"key": KEY.public_key()})()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    monkeypatch.setattr(ca, "_jwks", lambda team: _Keys())
    with patch.object(appmod, "APP_PASSWORD", "test-password"):
        yield appmod.app.test_client()


def test_valid_cloudflare_proof_logs_in_without_password(client):
    response = client.get("/", headers={ca.HEADER: token()})
    assert response.status_code == 200
    with client.session_transaction() as session:
        assert session.get("authenticated") is True


@pytest.mark.parametrize("bad", [lambda: token(aud="another-app"), lambda: token(iss="https://other.cloudflareaccess.com"),
                                 lambda: token(exp=int(time.time()) - 3600), lambda: token(key=OTHER), lambda: "forged"])
def test_wrong_proof_still_asks_for_the_password(client, bad):
    response = client.get("/", headers={ca.HEADER: bad()})
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_old_password_login_still_works(client):
    assert client.get("/").status_code == 302
    response = client.post("/login", data={"password": "test-password"})
    assert response.status_code == 302
    assert client.get("/").status_code == 200
    assert client.post("/login", data={"password": "wrong"}).status_code in (200, 302)


def test_without_the_setting_nothing_changes(monkeypatch):
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    monkeypatch.setattr(ca, "_jwks", lambda team: _Keys())
    with patch.object(appmod, "APP_PASSWORD", "test-password"):
        response = appmod.app.test_client().get("/", headers={ca.HEADER: token()})
    assert response.status_code == 302
