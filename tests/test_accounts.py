"""Saved jobs, who can open them, accounts, run limits and deleting. No API calls."""
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app as web
import auth
import tools
from storage import INTERRUPTED_MESSAGE, UNFINISHED, Store

CSV = b"id,pay\n1,100\n2,abc\n"
SCHEMA = '{"name": "t", "columns": {"pay": {"dtype": "int"}}}'


def scripted_agent(df, schema, output_path, mode, on_log):
    tools.set_dataframe(df)
    tools._state["error_log"].append(
        {"row_index": 1, "row_id": "2", "column": "pay", "original_value": "abc", "error": "bad pay"})
    on_log({"type": "start"})
    pd.DataFrame({"id": [1, 2], "pay": ["100", "abc"], "_errors": ["", "bad pay"]}).to_csv(output_path, index=False)


@pytest.fixture
def store(monkeypatch, tmp_path):
    s = Store(tmp_path)
    monkeypatch.setattr(web, "store", s)
    monkeypatch.setattr(web, "_active_job", None)
    monkeypatch.setattr(web, "run_agent", scripted_agent)
    monkeypatch.setattr(auth, "AUTH_MODE", "off")
    return s


@pytest.fixture
def accounts(monkeypatch, store):
    monkeypatch.setattr(auth, "AUTH_MODE", "dev")


def browser():
    """A separate browser: its own session cookie, and its own sign-in."""
    return TestClient(web.app)


def sign_in(client, email="ana@example.com"):
    response = client.post("/auth/dev-sign-in", data={"email": email}, follow_redirects=False)
    assert response.status_code == 303
    return client


def upload(client, contents=CSV):
    return client.post("/upload", files={"csv_file": ("pay.csv", contents, "text/csv")})


def run(client, upload_id=None):
    upload_id = upload_id or upload(client).json()["upload_id"]
    response = client.post("/clean", data={"upload_id": upload_id, "schema_json": SCHEMA})
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    for _ in range(200):
        if web.store.get_job(job_id, None)["status"] not in UNFINISHED:
            return job_id
        time.sleep(0.01)
    raise AssertionError("the run didn't finish")


# ── Saved jobs ──

def test_a_finished_job_survives_reopening_the_database(store, tmp_path):
    client = browser()
    job_id = run(client)
    owner = store.get_job(job_id, None)["owner"]

    reopened = Store(tmp_path)

    assert reopened.get_job(job_id, owner)["status"] == "done"
    assert [e["type"] for _, e in reopened.events(job_id)] == ["start", "done"]
    assert reopened.load_review(job_id)["flagged_rows"] == 1


def test_a_job_left_running_is_marked_interrupted_on_startup(store, tmp_path):
    upload_row = store.add_upload("session:x", "pay.csv", CSV, 2, ["id", "pay"], keep_seconds=60)
    job = store.add_job("session:x", upload_row, "clean", {}, keep_seconds=60)
    store.add_event(job["id"], {"type": "start"})

    reopened = Store(tmp_path)

    after = reopened.get_job(job["id"], "session:x")
    assert after["status"] == "interrupted"
    assert reopened.events(job["id"])[-1][1] == {"type": "error", "message": INTERRUPTED_MESSAGE}


def test_the_job_list_shows_only_this_browsers_runs(store):
    mine, theirs = browser(), browser()
    job_id = run(mine)
    run(theirs)

    listed = mine.get("/api/jobs").json()

    assert [j["job_id"] for j in listed] == [job_id]
    assert listed[0]["filename"] == "pay.csv"
    assert listed[0]["flagged_rows"] == 1


@pytest.mark.parametrize("request_call", [
    lambda c, u, j: c.get(f"/uploads/{u}"),
    lambda c, u, j: c.post("/infer-schema", data={"upload_id": u}),
    lambda c, u, j: c.post("/clean", data={"upload_id": u, "schema_json": SCHEMA}),
    lambda c, u, j: c.get(f"/stream/{j}"),
    lambda c, u, j: c.get(f"/review/{j}"),
    lambda c, u, j: c.get(f"/download/{j}"),
    lambda c, u, j: c.get(f"/download/{j}?file=report"),
    lambda c, u, j: c.get(f"/api/jobs/{j}"),
    lambda c, u, j: c.delete(f"/api/jobs/{j}"),
])
def test_another_browser_cannot_open_an_upload_or_job(store, request_call):
    owner = browser()
    upload_id = upload(owner).json()["upload_id"]
    job_id = run(owner, upload_id)

    response = request_call(browser(), upload_id, job_id)

    assert response.status_code == 404
    assert store.get_job(job_id, None) is not None


def test_deleting_a_job_removes_its_files(store):
    client = browser()
    job_id = run(client)
    assert store.output_path(job_id).exists()

    assert client.delete(f"/api/jobs/{job_id}").status_code == 200

    assert store.get_job(job_id, None) is None
    assert not store.job_dir(job_id).exists()
    assert store.events(job_id) == []
    assert client.get(f"/review/{job_id}").status_code == 404


def test_expired_jobs_and_their_files_are_deleted(store, monkeypatch):
    client = browser()
    job_id = run(client)
    later = time.time() + web.JOB_FILE_TTL_SECONDS + 1
    monkeypatch.setattr(time, "time", lambda: later)

    assert client.get("/api/jobs").json() == []
    assert not store.job_dir(job_id).exists()


def test_without_accounts_anyone_can_upload_and_there_is_no_sign_in(store):
    client = browser()

    assert upload(client).status_code == 200
    assert client.post("/auth/dev-sign-in", data={"email": "a@b.co"}).status_code == 404
    assert client.get("/me").json()["accounts"] is False


# ── Accounts ──

def test_signing_in_is_optional_and_runs_without_it_are_kept_an_hour(accounts):
    client = browser()

    job_id = run(client)

    job = web.store.get_job(job_id, None)
    assert job["owner"].startswith("session:")
    assert job["expires_at"] - job["created_at"] == pytest.approx(web.JOB_FILE_TTL_SECONDS, abs=5)
    assert client.get("/me").json()["run_limit"] is None


def test_the_browser_session_ends_when_the_browser_closes(accounts):
    response = browser().get("/me")

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(auth.SESSION_COOKIE + "=")
    assert "max-age" not in cookie.lower() and "expires" not in cookie.lower()


def test_the_demo_works_without_signing_in(accounts):
    client = browser()
    upload_id = client.post("/upload-demo").json()["upload_id"]

    job_id = run(client, upload_id)

    assert client.get(f"/review/{job_id}").status_code == 200


def test_a_signed_in_user_sees_their_own_runs_on_any_device(accounts):
    laptop, phone = sign_in(browser()), sign_in(browser())
    job_id = run(laptop)

    assert [j["job_id"] for j in phone.get("/api/jobs").json()] == [job_id]
    assert phone.get(f"/review/{job_id}").status_code == 200


def test_another_user_cannot_open_a_users_job(accounts):
    job_id = run(sign_in(browser(), "ana@example.com"))
    other = sign_in(browser(), "ben@example.com")

    assert other.get(f"/review/{job_id}").status_code == 404
    assert other.get(f"/download/{job_id}").status_code == 404
    assert other.get("/api/jobs").json() == []


def test_runs_from_before_signing_in_move_to_the_account(accounts):
    client = browser()
    demo_id = client.post("/upload-demo").json()["upload_id"]
    job_id = run(client, demo_id)

    sign_in(client)

    assert [j["job_id"] for j in client.get("/api/jobs").json()] == [job_id]


def test_an_edited_sign_in_cookie_is_ignored(accounts):
    sign_in(browser())  # the account exists
    client = browser()
    user_id = auth.dev_user_id("ana@example.com")
    client.cookies.set(auth.USER_COOKIE, f"{user_id}.0000")

    assert client.get("/me").json()["signed_in"] is False
    job_id = run(client)
    assert web.store.get_job(job_id, None)["owner"].startswith("session:")


def test_account_runs_are_kept_for_seven_days(accounts):
    client = sign_in(browser())
    job_id = run(client)

    job = web.store.get_job(job_id, None)

    assert job["expires_at"] - job["created_at"] == pytest.approx(7 * 24 * 3600, abs=5)
    assert client.get("/me").json()["keep_hours"] == 7 * 24


def test_the_monthly_run_limit_is_enforced_but_the_demo_is_free(accounts, monkeypatch):
    monkeypatch.setitem(web.RUNS_PER_MONTH, "free", 2)
    client = sign_in(browser())
    run(client)
    run(client)

    refused = client.post("/clean", data={"upload_id": upload(client).json()["upload_id"], "schema_json": SCHEMA})

    assert refused.status_code == 429
    assert "all 2 runs" in refused.json()["detail"]
    assert client.get("/me").json()["runs_this_month"] == 2
    run(client, client.post("/upload-demo").json()["upload_id"])


def test_deleting_an_account_removes_everything_it_had(accounts):
    client = sign_in(browser())
    upload_id = upload(client).json()["upload_id"]
    job_id = run(client, upload_id)
    user_id = auth.dev_user_id("ana@example.com")

    response = client.delete("/account")

    assert response.status_code == 200
    assert web.store.get_user(user_id) is None
    assert web.store.get_job(job_id, None) is None
    assert not web.store.job_dir(job_id).exists()
    assert not web.store.upload_path(upload_id).exists()
    assert client.get("/me").json()["signed_in"] is False


def test_deleting_an_account_needs_a_sign_in(accounts):
    assert browser().delete("/account").status_code == 401
