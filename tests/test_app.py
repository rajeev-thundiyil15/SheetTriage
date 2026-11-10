"""Tests for the upload, schema and run routes. No API calls: the agent is replaced."""
import time

import pytest
from fastapi.testclient import TestClient

import app as web
import auth
from storage import UNFINISHED, Store

CSV = b"id,signed\n1,03/04/2024\n2,05/06/2024\n"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "store", Store(tmp_path))
    monkeypatch.setattr(web, "_active_job", None)
    monkeypatch.setattr(auth, "AUTH_MODE", "off")
    return TestClient(web.app)


def upload(client, contents=CSV, name="orders.csv"):
    return client.post("/upload", files={"csv_file": (name, contents, "text/csv")})


def stored_uploads():
    return [u["id"] for u in web.store.count_uploads()]


def wait_for(job_id):
    for _ in range(200):
        job = web.store.get_job(job_id, None)
        if job["status"] not in UNFINISHED:
            return job
        time.sleep(0.01)
    raise AssertionError("the run didn't finish")


@pytest.mark.parametrize("path", ["/app", "/schema", "/results"])
def test_each_step_has_a_page(client, path):
    response = client.get(path)

    assert response.status_code == 200
    assert 'class="steps"' in response.text


def test_upload_returns_a_summary(client):
    body = upload(client).json()

    assert body["filename"] == "orders.csv"
    assert body["rows"] == 2
    assert body["columns"] == ["id", "signed"]
    assert client.get(f"/uploads/{body['upload_id']}").json() == body


def test_upload_rejects_an_unreadable_file(client):
    assert upload(client, b"").status_code == 400


def test_upload_enforces_the_row_limit(client, monkeypatch):
    monkeypatch.setattr(web, "MAX_UPLOAD_ROWS", 1)

    assert upload(client).status_code == 413
    assert not stored_uploads()


def test_oldest_upload_is_dropped_past_the_limit(client, monkeypatch):
    monkeypatch.setattr(web, "MAX_STORED_UPLOADS", 2)
    first = upload(client).json()["upload_id"]
    time.sleep(0.01)
    second = upload(client).json()["upload_id"]
    time.sleep(0.01)

    third = upload(client).json()["upload_id"]

    assert set(stored_uploads()) == {second, third}
    assert not web.store.upload_path(first).exists()


def test_old_uploads_expire(client, monkeypatch):
    upload_id = upload(client).json()["upload_id"]
    monkeypatch.setattr(time, "time", lambda real=time.time: real() + web.JOB_FILE_TTL_SECONDS + 1)

    upload(client)

    assert upload_id not in stored_uploads()
    assert not web.store.upload_path(upload_id).exists()


@pytest.mark.parametrize("request_call", [
    lambda c: c.get("/uploads/nope"),
    lambda c: c.post("/infer-schema", data={"upload_id": "nope"}),
    lambda c: c.post("/clean", data={"upload_id": "nope", "schema_json": '{"name": "t", "columns": {}}'}),
])
def test_unknown_uploads_get_a_clear_404(client, request_call):
    response = request_call(client)

    assert response.status_code == 404
    assert "upload your CSV again" in response.json()["detail"]


def test_infer_schema_scans_the_stored_upload(client):
    upload_id = upload(client).json()["upload_id"]

    body = client.post("/infer-schema", data={"upload_id": upload_id}).json()

    assert body["schema"]["columns"]["signed"]["dtype"] == "date"
    assert body["questions"][0]["id"] == "date_format:signed"


def test_placeholder_text_is_not_treated_as_blank(client):
    contents = b"id,pay\n" + b"".join(b"%d,%d\n" % (i, i * 10) for i in range(1, 10)) + b"10,n/a\n11,\n"
    upload_id = upload(client, contents).json()["upload_id"]

    body = client.post("/infer-schema", data={"upload_id": upload_id}).json()
    questions = {q["id"]: q["question"] for q in body["questions"]}

    assert "'n/a'" in questions["not_number:pay"]
    assert "1 row(s)" in questions["blank:pay"]


def test_stream_replays_the_whole_run_on_every_connection(client, monkeypatch):
    def scripted_agent(df, schema, output_path, mode, on_log):
        on_log({"type": "start"})
        on_log({"type": "summary", "clean_rows": 2})
    monkeypatch.setattr(web, "run_agent", scripted_agent)
    upload_id = upload(client).json()["upload_id"]
    schema = '{"name": "t", "columns": {"id": {"dtype": "int"}}}'
    job_id = client.post("/clean", data={"upload_id": upload_id, "schema_json": schema}).json()["job_id"]
    wait_for(job_id)

    first = client.get(f"/stream/{job_id}").text
    second = client.get(f"/stream/{job_id}").text

    assert first == second
    assert first.count("data: ") == 3
    assert '"type": "done"' in second


def test_clean_runs_the_agent_on_the_stored_upload(client, monkeypatch):
    received = {}

    def fake_run_agent(df, schema, output_path, mode, on_log):
        received.update(rows=len(df), mode=mode, date_format=schema.columns["signed"].date_format)

    monkeypatch.setattr(web, "run_agent", fake_run_agent)
    upload_id = upload(client).json()["upload_id"]
    schema = '{"name": "t", "columns": {"signed": {"dtype": "date", "date_format": "DMY"}}}'

    response = client.post("/clean", data={"upload_id": upload_id, "schema_json": schema, "mode": "strict"})
    wait_for(response.json()["job_id"])

    assert received == {"rows": 2, "mode": "strict", "date_format": "DMY"}


# Files as Excel and other tools save them

@pytest.mark.parametrize("contents, columns, first_row", [
    ("name,city\nJosé,München\n".encode("cp1252"), ["name", "city"], ["José", "München"]),
    ("﻿name,age\nAna,30\n".encode("utf-8"), ["name", "age"], ["Ana", "30"]),
    (b"name;age;city\nAna;30;Rome\n", ["name", "age", "city"], ["Ana", "30", "Rome"]),
    (b"name\tage\nAna\t30\n", ["name", "age"], ["Ana", "30"]),
    (b'name,amount\n"Silva; Ana","1;5"\n', ["name", "amount"], ["Silva; Ana", "1;5"]),
    (b"a,b\n1,2,,\n", ["a", "b"], ["1", "2"]),
])
def test_upload_reads_common_csv_variants(client, contents, columns, first_row):
    body = upload(client, contents).json()

    assert body["columns"] == columns
    assert web._read_csv(web.store.upload_contents(body["upload_id"]), dtype=str).iloc[0].tolist() == first_row


@pytest.mark.parametrize("contents, message", [
    (b"", "empty"),
    (b"a,b,c\n", "no data rows"),
    (b"a,b\n1,2\n3,4,5,6\n", "Row 3 has 4 values"),
    (b"\x00\x01\x02binary", "doesn't look like a CSV"),
])
def test_upload_refuses_files_it_cannot_read_safely(client, contents, message):
    response = upload(client, contents)

    assert response.status_code == 400
    assert message in response.json()["detail"]
    assert not stored_uploads()


def _api_error(cls_name, status, message="error"):
    import anthropic
    import httpx
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return getattr(anthropic, cls_name)(message, response=response, body=None)


@pytest.mark.parametrize("make_error, expected", [
    (lambda: _api_error("InternalServerError", 529), "overloaded"),
    (lambda: _api_error("RateLimitError", 429), "usage limit"),
    (lambda: _api_error("AuthenticationError", 401), "isn't set up correctly"),
    (lambda: _api_error("BadRequestError", 400, "Your credit balance is too low"), "spending limit"),
    (lambda: RuntimeError("disk full"), "unexpected error: disk full"),
])
def test_run_errors_are_explained_in_plain_words(make_error, expected):
    message = web._run_error_message(make_error())

    assert expected in message
    assert "Error code" not in message


def test_a_failed_run_ends_with_a_plain_error_event(client, monkeypatch):
    def failing_agent(*args, on_log, **kwargs):
        on_log({"type": "start"})
        raise _api_error("InternalServerError", 529)
    monkeypatch.setattr(web, "run_agent", failing_agent)
    upload_id = upload(client).json()["upload_id"]
    schema = '{"name": "t", "columns": {"id": {"dtype": "int"}}}'

    job_id = client.post("/clean", data={"upload_id": upload_id, "schema_json": schema}).json()["job_id"]
    job = wait_for(job_id)

    _, last = web.store.events(job_id)[-1]
    assert last["type"] == "error"
    assert "overloaded" in last["message"]
    assert job["status"] == "error"
    assert job["error"] == last["message"]
