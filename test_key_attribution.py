"""キー本体を返さず、認証なしでは診断を公開しない。外部APIなし。"""
from unittest.mock import patch

import app as appmod


def test_key_attribution_requires_login_and_never_returns_secret():
    with patch.object(appmod, "APP_PASSWORD", "test-password"), \
         patch.dict(appmod.os.environ, {"GEMINI_API_KEY": "fake-diagram-key-ABCD"}):
        client = appmod.app.test_client()
        assert client.get("/api/key-attribution").status_code == 302
        with client.session_transaction() as session:
            session["authenticated"] = True
        response = client.get("/api/key-attribution")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.get_json()["key_suffix"] == "ABCD"
        assert "fake-diagram-key-ABCD" not in response.get_data(as_text=True)
    with patch.object(appmod, "APP_PASSWORD", ""):
        assert appmod.app.test_client().get("/api/key-attribution").status_code == 403
