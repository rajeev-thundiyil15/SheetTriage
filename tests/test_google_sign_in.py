"""Signing in with Google. Google's token check is replaced, so no network is used."""
import pytest
from fastapi.testclient import TestClient

import app as web
import auth
from storage import Store

CLIENT_ID = "test-client.apps.googleusercontent.com"
CSV = b"id,pay\n1,100\n"


def payload(**overrides):
    return {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "1234567890",
            "email": "ana@example.com", "email_verified": True, **overrides}


@pytest.fixture
def google(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "store", Store(tmp_path))
    monkeypatch.setattr(auth, "AUTH_MODE", "google")
    monkeypatch.setattr(auth, "GOOGLE_CLIENT_ID", CLIENT_ID)
    tokens = {"good-token": payload()}

    def check(credential):
        if credential not in tokens:
            raise ValueError("Could not verify token signature.")
        return tokens[credential]
    monkeypatch.setattr(auth, "_google_payload", check)
    return tokens


def sign_in(client, credential="good-token", **kwargs):
    return client.post("/auth/google", json={"credential": credential, "next": "/app"}, **kwargs)


def test_a_valid_google_sign_in_signs_the_user_in(google):
    client = TestClient(web.app)

    response = sign_in(client)

    assert response.status_code == 200
    assert response.json() == {"next": "/app"}
    me = client.get("/me").json()
    assert me["signed_in"] is True
    assert me["email"] == "ana@example.com"
    assert client.post("/upload", files={"csv_file": ("a.csv", CSV, "text/csv")}).status_code == 200


def test_the_account_is_keyed_by_googles_id_not_the_email(google):
    google["new-email-token"] = payload(email="ana.new@example.com")
    sign_in(TestClient(web.app))

    sign_in(TestClient(web.app), "new-email-token")

    user = web.store.get_user("google_1234567890")
    assert user["email"] == "ana.new@example.com"


@pytest.mark.parametrize("token_payload, message", [
    (payload(aud="someone-elses-app"), "couldn't be confirmed"),
    (payload(iss="https://evil.example.com"), "couldn't be confirmed"),
    (payload(email_verified=False), "isn't verified"),
    (payload(email=""), "isn't verified"),
])
def test_untrustworthy_google_tokens_are_refused(google, token_payload, message):
    google["bad-token"] = token_payload
    client = TestClient(web.app)

    response = sign_in(client, "bad-token")

    assert response.status_code == 401
    assert message in response.json()["detail"]
    assert client.get("/me").json()["signed_in"] is False


def test_a_forged_or_expired_token_is_refused(google):
    client = TestClient(web.app)

    response = sign_in(client, "forged-token")

    assert response.status_code == 401
    assert client.get("/me").json()["signed_in"] is False


def test_sign_in_must_be_sent_as_json_from_this_site(google):
    client = TestClient(web.app)

    as_form = client.post("/auth/google", data={"credential": "good-token"})
    from_another_site = sign_in(client, headers={"Origin": "https://evil.example.com"})

    assert as_form.status_code == 415
    assert from_another_site.status_code == 403
    assert client.get("/me").json()["signed_in"] is False


def test_sign_in_only_sends_people_back_to_this_site(google):
    response = TestClient(web.app).post(
        "/auth/google", json={"credential": "good-token", "next": "//evil.example.com"})

    assert response.json() == {"next": "/files"}


def test_the_pages_get_the_client_id_and_the_dev_sign_in_is_off(google):
    client = TestClient(web.app)

    me = client.get("/me").json()

    assert me["auth_mode"] == "google"
    assert me["google_client_id"] == CLIENT_ID
    assert client.post("/auth/dev-sign-in", data={"email": "a@b.co"}).status_code == 404


def test_google_sign_in_is_off_unless_chosen(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "store", Store(tmp_path))
    monkeypatch.setattr(auth, "AUTH_MODE", "off")

    assert sign_in(TestClient(web.app)).status_code == 404


def test_the_real_check_asks_google_for_this_apps_audience(monkeypatch):
    from google.oauth2 import id_token
    seen = {}

    def fake_verify(token, request, audience=None, clock_skew_in_seconds=0):
        seen.update(token=token, audience=audience)
        return payload()
    monkeypatch.setattr(auth, "GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(id_token, "verify_oauth2_token", fake_verify)

    assert auth.google_user("a-token") == ("google_1234567890", "ana@example.com")
    assert seen == {"token": "a-token", "audience": CLIENT_ID}
