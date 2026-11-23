"""A visitor's own Anthropic key: checked first, used for the whole run, never stored. No API calls."""
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import agent
import app as web
import auth
import costs
import tools
from storage import UNFINISHED, Store

CSV = b"id,status\n1,Shipped\n2,Shiped\n"
SCHEMA = '{"name": "t", "columns": {"status": {"dtype": "str", "allowed_values": ["Pending", "Shipped"]}}}'
KEY = "sk-ant-api03-" + "A1b2_C3d4-" * 8


class ApiError(Exception):
    def __init__(self, status_code, message="error"):
        super().__init__(message)
        self.status_code = status_code


@pytest.fixture
def store(monkeypatch, tmp_path):
    s = Store(tmp_path)
    monkeypatch.setattr(web, "store", s)
    monkeypatch.setattr(auth, "AUTH_MODE", "off")
    monkeypatch.setattr(web, "check_own_key", lambda key: None)
    return s


@pytest.fixture
def keys_seen(monkeypatch):
    """Which key each run's agent and suggestion ranking were given (None: the server's)."""
    seen = {"agent": [], "rank": []}

    def agent_run(df, schema, output_path, mode, on_log, api_key=None):
        # Flags 'Shiped' and changes nothing, so the suggestions step has a fix to ask Claude about.
        seen["agent"].append(api_key)
        tools.set_dataframe(df)
        on_log({"type": "start"})
        tools.mark_remaining_violations("status", schema)
        tools.save_with_report(output_path, output_path.replace("_clean.csv", "_report.csv"))

    def rank(cells, on_log=None, api_key=None):
        seen["rank"].append(api_key)
        return {}

    monkeypatch.setattr(web, "run_agent", agent_run)
    monkeypatch.setattr(web.fixes, "rank_with_ai", rank)
    return seen


def start(client, api_key=None):
    upload_id = client.post("/upload", files={"csv_file": ("pay.csv", CSV, "text/csv")}).json()["upload_id"]
    data = {"upload_id": upload_id, "schema_json": SCHEMA}
    if api_key is not None:
        data["api_key"] = api_key
    return client.post("/clean", data=data)


def finish(job_id):
    for _ in range(300):
        if web.store.get_job(job_id, None)["status"] not in UNFINISHED:
            return web.store.get_job(job_id, None)
        time.sleep(0.01)
    raise AssertionError("the run didn't finish")


def test_a_run_on_your_own_key_uses_it_for_the_agent_and_the_suggestions(store, keys_seen):
    response = start(TestClient(web.app), KEY)

    assert response.status_code == 200, response.text
    job = finish(response.json()["job_id"])
    assert job["status"] == "done"
    assert job["own_key"] is True
    assert keys_seen["agent"] == [KEY]
    assert keys_seen["rank"] and set(keys_seen["rank"]) == {KEY}


def test_without_a_key_the_run_uses_the_servers(store, keys_seen):
    job = finish(start(TestClient(web.app)).json()["job_id"])

    assert job["own_key"] is False
    assert keys_seen["agent"] == [None]
    assert set(keys_seen["rank"]) == {None}


def test_runs_on_your_own_key_dont_count_and_still_work_once_the_free_runs_are_used(store, keys_seen, monkeypatch):
    monkeypatch.setattr(web, "FREE_RUNS_PER_DAY", 1)
    client = TestClient(web.app)
    finish(start(client, KEY).json()["job_id"])
    finish(start(client).json()["job_id"])  # the one free run: the own-key run above didn't use it

    refused = start(client)
    with_key = start(client, KEY)

    assert refused.status_code == 429
    assert "your own Anthropic API key" in refused.json()["detail"]
    assert with_key.status_code == 200
    assert client.get("/me").json()["runs_used"] == 1


def test_the_key_is_never_written_to_the_database_or_the_runs_files(store, keys_seen, tmp_path):
    job = finish(start(TestClient(web.app), KEY).json()["job_id"])
    assert job["status"] == "done"

    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), f"the key was written to {path.name}"


def test_something_that_isnt_a_key_is_refused_without_asking_anthropic(store, keys_seen, monkeypatch):
    asked = []
    monkeypatch.setattr(web, "check_own_key", lambda key: asked.append(key))

    response = start(TestClient(web.app), "my-password")

    assert response.status_code == 400
    assert response.headers["X-Problem"] == "api-key"
    assert "sk-ant-" in response.json()["detail"]
    assert asked == []
    assert keys_seen["agent"] == []


def test_a_key_anthropic_refuses_stops_the_run_before_it_starts(store, keys_seen, monkeypatch):
    monkeypatch.setattr(web, "check_own_key", lambda key: "Your Anthropic account is out of credit.")

    response = start(TestClient(web.app), KEY)

    assert response.status_code == 400
    assert response.headers["X-Problem"] == "api-key"
    assert response.json()["detail"] == "Your Anthropic account is out of credit."
    assert store.count_jobs("queued") == store.count_jobs("running") == 0
    assert keys_seen["agent"] == []


def test_a_run_that_fails_on_your_key_says_its_your_account(store, monkeypatch):
    def out_of_credit(df, schema, output_path, mode, on_log, api_key=None):
        raise ApiError(400, "Your credit balance is too low to access the Anthropic API.")

    monkeypatch.setattr(web, "run_agent", out_of_credit)

    job = finish(start(TestClient(web.app), KEY).json()["job_id"])

    assert job["status"] == "error"
    assert job["error"].startswith("Your Anthropic account is out of credit")


def test_an_unexpected_error_never_shows_the_key():
    message = web._run_error_message(ValueError(f"bad header {KEY}"), KEY)

    assert KEY not in message
    assert "[your API key]" in message


@pytest.mark.parametrize("error, says", [
    (ApiError(401), "didn't accept that API key"),
    (ApiError(403), "isn't allowed"),
    (ApiError(400, "Your credit balance is too low"), "out of credit"),
    (ApiError(429), "rate limit"),
    (ApiError(529), "overloaded"),
])
def test_key_problems_are_explained_in_plain_words(error, says):
    assert says in agent.own_key_problem(error)


def test_checking_a_key_makes_one_tiny_request_with_it(monkeypatch):
    made = {}

    def client(api_key=None, max_retries=None):
        made["key"] = api_key
        return SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: made.update(request=kw)))

    monkeypatch.setattr(agent, "claude_client", client)

    assert agent.check_own_key(KEY) is None
    assert made["key"] == KEY
    assert made["request"]["max_tokens"] == 1


def test_checking_a_bad_key_says_whats_wrong(monkeypatch):
    def refuse(**kw):
        raise ApiError(401)

    monkeypatch.setattr(agent, "claude_client",
                        lambda api_key=None, max_retries=None: SimpleNamespace(messages=SimpleNamespace(create=refuse)))

    assert "didn't accept" in agent.check_own_key(KEY)


def test_your_own_key_gets_more_retries_for_low_rate_limits():
    assert agent.claude_client(KEY).max_retries == 6
    assert agent.claude_client(KEY, max_retries=1).max_retries == 1


def test_the_cost_report_leaves_out_runs_on_visitors_own_keys(store):
    upload = store.add_upload("session:x", "pay.csv", CSV, rows=2, columns=["id", "pay"], keep_seconds=3600)
    for own_key in (False, True):
        job = store.add_job("session:x", upload, "clean", {}, keep_seconds=3600, own_key=own_key)
        store.add_usage(job["id"], {"model": "claude-haiku-4-5", "input_tokens": 1_000_000,
                                    "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0})

    text = costs.report(store, days=1)

    assert "1 runs, 1 with a known price, $1.00 in total." in text
    assert "1 more runs used visitors' own API keys" in text
