"""Tests for the upload, schema and run routes. No API calls: the agent is replaced."""
import time

import pytest
from fastapi.testclient import TestClient

import app as web

CSV = b"id,signed\n1,03/04/2024\n2,05/06/2024\n"


@pytest.fixture
def client(monkeypatch):
    web._uploads.clear()
    monkeypatch.setattr(web, "_active_job", None)
    return TestClient(web.app)


def upload(client, contents=CSV, name="orders.csv"):
    return client.post("/upload", files={"csv_file": (name, contents, "text/csv")})


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
    assert not web._uploads


def test_oldest_upload_is_dropped_past_the_limit(client, monkeypatch):
    monkeypatch.setattr(web, "MAX_STORED_UPLOADS", 2)
    first = upload(client).json()["upload_id"]
    second = upload(client).json()["upload_id"]
    web._uploads[first]["created_at"] -= 10

    third = upload(client).json()["upload_id"]

    assert set(web._uploads) == {second, third}


def test_old_uploads_expire(client):
    upload_id = upload(client).json()["upload_id"]
    web._uploads[upload_id]["created_at"] = time.time() - web.JOB_FILE_TTL_SECONDS - 1

    upload(client)

    assert upload_id not in web._uploads


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


def test_stream_replays_the_whole_run_on_every_connection(client):
    events = [{"type": "start"}, {"type": "summary", "clean_rows": 2}, {"type": "done"}]
    web._jobs["j"] = {"events": events, "output_path": "x.csv", "done": True, "created_at": time.time()}

    first = client.get("/stream/j").text
    second = client.get("/stream/j").text

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
    job_id = response.json()["job_id"]
    for _ in range(100):
        if web._jobs[job_id]["done"]:
            break
        time.sleep(0.01)

    assert received == {"rows": 2, "mode": "strict", "date_format": "DMY"}
