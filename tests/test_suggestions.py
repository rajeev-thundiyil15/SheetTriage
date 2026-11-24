"""Suggested fixes for flagged cells, duplicates, and applying a person's decisions. No API calls."""
import io
import json
import time
from collections import Counter

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app as web
import auth
import suggestions as fixes
import tools
from schema import DataSchema
from storage import UNFINISHED, Store

SCHEMA = DataSchema.model_validate({"name": "orders", "columns": {
    "order_id": {"dtype": "int", "nullable": False},
    "status": {"dtype": "str", "nullable": False, "allowed_values": ["Pending", "Shipped", "Delivered", "Cancelled"]},
    "payment": {"dtype": "str", "nullable": False, "allowed_values": ["Card", "PayPal", "Bank Transfer"]},
    "price": {"dtype": "float", "nullable": False, "min_value": 0, "max_value": 3000},
    "quantity": {"dtype": "int", "nullable": False, "min_value": 1, "max_value": 20},
    "ordered": {"dtype": "date", "nullable": False, "date_format": "MDY"},
    "email": {"dtype": "str", "nullable": False, "pattern": ".+@.+\\..+"},
}})


def options(value, issue, column):
    return [o["value"] for o in fixes.cell_options(value, issue, SCHEMA.columns[column], Counter())]


# ── Options the code finds ──

@pytest.mark.parametrize("value, issue, column, expected", [
    ("Credit Card", "'Credit Card' not in allowed values", "payment", ["Card"]),
    ("Delivred", "'Delivred' not in allowed values", "status", ["Delivered"]),
    ("Bitcoin", "'Bitcoin' not in allowed values", "payment", []),
    ("Bank transfer", "'Bank transfer' not in allowed values", "payment", ["Bank Transfer"]),
    ("31/01/2026", "cannot convert '31/01/2026' to date", "ordered", ["2026-01-31"]),
    ("02/29/2026", "cannot convert '02/29/2026' to date", "ordered", ["2026-02-28", "2026-03-01"]),
    ("€249.99", "cannot convert '€249.99' to float", "price", ["249.99"]),
    ("1,250.00", "cannot convert '1,250.00' to float", "price", ["1250.00"]),
    ("call for price", "cannot convert 'call for price' to float", "price", []),
    ("three", "cannot convert 'three' to int", "quantity", ["3"]),
    ("1.5", "cannot convert '1.5' to int", "quantity", ["1", "2"]),
    ("-29.99", "value -29.99 is below min 0.0", "price", ["29.99"]),
    ("4500.0", "value 4500.0 is above max 3000.0", "price", []),
    ("25", "value 25 is above max 20.0", "quantity", []),
    ("(blank)", "null value not allowed", "email", []),
    ("ana @example.com", "'ana @example.com' does not match pattern", "email", ["ana@example.com"]),
    ("TBC", "'TBC' does not match pattern", "email", []),
])
def test_the_code_only_offers_fixes_it_can_back_up(value, issue, column, expected):
    assert options(value, issue, column) == expected


def test_a_date_option_tells_claude_the_columns_own_order():
    # Haiku 5.5 once called a month-first reading "the column's order" in a day-first column,
    # because the order was never given to it.
    [option] = fixes.cell_options("31/01/2026", "cannot convert '31/01/2026' to date", SCHEMA.columns["ordered"], Counter())

    assert option["note"] == "read day first; this column is written month first"


def test_a_number_option_must_still_be_in_range():
    assert options("€4,999.00", "cannot convert '€4,999.00' to float", "price") == []


# ── Duplicates ──

def test_rows_matching_in_every_column_but_the_id_are_duplicates():
    df = pd.DataFrame({
        "order_id": ["1", "2", "3", "4"],
        "customer": ["Ana", "Ben", "ana ", "Cy"],
        "product": ["Mouse", "Laptop", "mouse", "Mouse"],
    })

    groups = fixes.find_duplicates(df)

    assert groups == [{"positions": [0, 2], "ignored": ["order_id"]}]


def test_a_column_named_like_an_id_with_repeats_still_counts():
    df = pd.DataFrame({"code": ["A", "A"], "name": ["x", "y"]})

    assert fixes.find_duplicates(df) == []


# ── Claude's recommendation ──

def report_entry(row, column, value, issue):
    return {"row": row, "row_id": str(row), "column": column, "value": value, "issue": issue}


def test_a_recommendation_outside_the_options_is_ignored():
    output = pd.DataFrame({"payment": ["Card", "Credit Card"], "status": ["Pending", "shiped"]})
    report = [report_entry(3, "payment", "Credit Card", "'Credit Card' not in allowed values"),
              report_entry(3, "status", "shiped", "'shiped' not in allowed values")]

    def rank(cells):
        return {cells[0]["id"]: (5, "made up"), cells[1]["id"]: (0, "Close misspelling of Shipped.")}

    found = fixes.build_suggestions(output, report, SCHEMA, rank=rank)

    assert found[0]["recommended"] is None
    assert found[1]["recommended"] == 0
    assert found[1]["reason"] == "Close misspelling of Shipped."


def test_cells_without_options_are_not_sent_to_claude():
    sent = []
    output = pd.DataFrame({"email": [""]})
    report = [report_entry(2, "email", "(blank)", "null value not allowed")]

    found = fixes.build_suggestions(output, report, SCHEMA, rank=lambda cells: sent.extend(cells) or {})

    assert sent == []
    assert found[0]["options"] == []


def test_rank_with_ai_reads_the_tool_answer_and_records_tokens(monkeypatch):
    from types import SimpleNamespace
    import anthropic
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", input={"fixes": [{"id": "c1", "pick": 0, "reason": "Close typo."}]})],
            usage=SimpleNamespace(input_tokens=500, output_tokens=40, cache_creation_input_tokens=0, cache_read_input_tokens=0))
    monkeypatch.setattr(anthropic, "Anthropic", lambda: SimpleNamespace(messages=SimpleNamespace(create=create)))
    logged = []
    cell = {"id": "c1", "column": "status", "value": "shiped", "issues": ["'shiped' not in allowed values"],
            "options": [{"value": "Shipped", "note": "closest allowed value"}]}

    picks = fixes.rank_with_ai([cell], on_log=logged.append)

    assert picks == {"c1": (0, "Close typo.")}
    assert calls[0]["tool_choice"] == {"type": "tool", "name": "recommend_fixes"}
    assert logged[0]["type"] == "usage" and logged[0]["input_tokens"] == 500


def test_rank_with_ai_gives_up_quietly_when_claude_is_unreachable(monkeypatch):
    import anthropic

    def broken():
        raise RuntimeError("no network")
    monkeypatch.setattr(anthropic, "Anthropic", broken)

    assert fixes.rank_with_ai([{"id": "c1", "column": "x", "value": "y", "issues": ["z"], "options": []}]) == {}


# ── A run, then decisions ──

CSV = (b"order_id,status,payment,price\n"
       b"1,Pending,Card,10.00\n"
       b"2,shiped,Credit Card,20.00\n"
       b"3,Pending,Card,10.00\n"
       b"4,Returned,PayPal,5.00\n")
RUN_SCHEMA = json.dumps({"name": "o", "columns": {
    "order_id": {"dtype": "int"},
    "status": {"dtype": "str", "allowed_values": ["Pending", "Shipped", "Delivered", "Cancelled"]},
    "payment": {"dtype": "str", "allowed_values": ["Card", "PayPal", "Bank Transfer"]},
    "price": {"dtype": "float"},
}})


def flagging_agent(df, schema, output_path, mode, on_log):
    """Flags every rule break and changes nothing, like an audit run."""
    tools.set_dataframe(df)
    for column in schema.columns:
        tools.mark_remaining_violations(column, schema)
    tools.save_with_report(output_path, output_path.replace("_clean.csv", "_report.csv"),
                           exclude_error_rows=mode == "strict")


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "store", Store(tmp_path))
    monkeypatch.setattr(auth, "AUTH_MODE", "off")
    monkeypatch.setattr(web, "run_agent", flagging_agent)
    monkeypatch.setattr(fixes, "rank_with_ai", lambda cells, on_log=None: {c["id"]: (0, "Most likely.") for c in cells})
    return TestClient(web.app)


def run(client, mode="clean"):
    upload_id = client.post("/upload", files={"csv_file": ("orders.csv", CSV, "text/csv")}).json()["upload_id"]
    job_id = client.post("/clean", data={"upload_id": upload_id, "schema_json": RUN_SCHEMA, "mode": mode}).json()["job_id"]
    for _ in range(300):
        if web.store.get_job(job_id, None)["status"] not in UNFINISHED:
            return job_id
        time.sleep(0.01)
    raise AssertionError("the run didn't finish")


def suggestion(review, kind, **match):
    return next(s for s in review["suggestions"] if s["kind"] == kind and all(s.get(k) == v for k, v in match.items()))


def test_a_run_comes_back_with_suggestions_and_duplicates(client):
    job_id = run(client)

    review = client.get(f"/review/{job_id}").json()

    status = suggestion(review, "cell", column="status", row=3)
    assert [o["value"] for o in status["options"]] == ["Shipped"]
    assert status["recommended"] == 0
    assert suggestion(review, "cell", column="payment", row=3)["options"][0]["value"] == "Card"
    assert suggestion(review, "cell", column="status", row=5)["options"] == []
    assert suggestion(review, "duplicate")["rows"] == [2, 4]
    assert review["flagged_rows"] == 2


def test_nothing_changes_until_someone_decides(client):
    job_id = run(client)

    lines = client.get(f"/download/{job_id}").text.splitlines()

    assert lines[2].startswith("2,shiped,Credit Card,")
    assert len(lines) == 5


def test_accepting_a_fix_changes_the_review_and_both_downloads(client):
    job_id = run(client)
    status = suggestion(client.get(f"/review/{job_id}").json(), "cell", column="status", row=3)

    review = client.post(f"/api/jobs/{job_id}/decisions", json={"id": status["id"], "action": "accept", "option": 0}).json()

    row = review["rows"][1]
    assert row["values"][1] == "Shipped"
    assert row["approved"] == {"status": "shiped"}
    assert "status" not in row["issues"] and "payment" in row["issues"]
    assert review["approved_cells"] == 1
    csv_lines = client.get(f"/download/{job_id}").text.splitlines()
    assert csv_lines[2].startswith("2,Shipped,Credit Card,")
    assert "shiped" not in csv_lines[2] and "Credit Card" in csv_lines[2].split(",", 4)[-1]
    report = client.get(f"/download/{job_id}?file=report").text
    assert "shiped" not in report and "Credit Card" in report


def test_keeping_a_value_clears_its_flag_without_changing_it(client):
    job_id = run(client)
    returned = suggestion(client.get(f"/review/{job_id}").json(), "cell", column="status", row=5)

    review = client.post(f"/api/jobs/{job_id}/decisions", json={"id": returned["id"], "action": "keep"}).json()

    row = review["rows"][3]
    assert row["values"][1] == "Returned"
    assert row["kept"] == ["status"] and row["issues"] == {}
    last = client.get(f"/download/{job_id}").text.splitlines()[4]
    assert last.startswith("4,Returned,PayPal,") and last.endswith(",")


def test_removing_a_duplicate_leaves_the_copy_out_of_the_download(client):
    job_id = run(client)
    copy = suggestion(client.get(f"/review/{job_id}").json(), "duplicate")

    review = client.post(f"/api/jobs/{job_id}/decisions", json={"id": copy["id"], "action": "remove"}).json()

    assert review["rows"][2]["removed_as_copy_of"] == 2
    assert review["removed_rows"] == 1
    ids = [line.split(",")[0] for line in client.get(f"/download/{job_id}").text.splitlines()[1:]]
    assert ids == ["1", "2", "4"]


def test_accept_all_recommended_and_undo(client):
    job_id = run(client)

    review = client.post(f"/api/jobs/{job_id}/decisions/recommended").json()

    assert review["approved_cells"] == 2
    status = suggestion(review, "cell", column="status", row=3)
    undone = client.post(f"/api/jobs/{job_id}/decisions", json={"id": status["id"], "action": "leave"}).json()
    assert undone["approved_cells"] == 1
    assert undone["rows"][1]["values"][1] == "shiped"


@pytest.mark.parametrize("body", [
    {"id": "c1", "action": "accept", "option": 9},
    {"id": "c1", "action": "accept"},
    {"id": "c1", "action": "remove"},
    {"id": "d1", "action": "accept", "option": 0},
])
def test_only_the_offered_choices_are_accepted(client, body):
    job_id = run(client)

    response = client.post(f"/api/jobs/{job_id}/decisions", json=body)

    assert response.status_code == 400


def test_another_browser_cannot_decide_for_someone(client):
    job_id = run(client)

    response = TestClient(web.app).post(f"/api/jobs/{job_id}/decisions", json={"id": "c1", "action": "keep"})

    assert response.status_code == 404


def test_strict_runs_have_nothing_to_decide(client):
    job_id = run(client, mode="strict")

    assert client.get(f"/review/{job_id}").json()["suggestions"] == []
    assert client.post(f"/api/jobs/{job_id}/decisions", json={"id": "c1", "action": "keep"}).status_code == 404


def test_cells_with_the_same_value_share_one_question_to_claude():
    output = pd.DataFrame({"payment": ["Credit Card", "Credit Card", "Credit Card"]})
    report = [report_entry(r, "payment", "Credit Card", "'Credit Card' not in allowed values") for r in (2, 3, 4)]
    asked = []

    def rank(cells):
        asked.extend(cells)
        return {cells[0]["id"]: (0, "Card is the only close payment type.")}

    found = [s for s in fixes.build_suggestions(output, report, SCHEMA, rank=rank) if s["kind"] == "cell"]

    assert len(asked) == 1
    assert [s["recommended"] for s in found] == [0, 0, 0]
    assert {s["reason"] for s in found} == {"Card is the only close payment type."}


def test_large_reviews_are_sent_compressed(client):
    job_id = run(client)

    response = client.get(f"/review/{job_id}", headers={"Accept-Encoding": "gzip"})

    assert response.headers.get("content-encoding") == "gzip"


def test_spelling_overlap_alone_is_not_a_suggestion():
    countries = DataSchema.model_validate({"name": "c", "columns": {
        "country": {"dtype": "str", "allowed_values": ["UK", "Ireland", "France", "Germany", "Spain", "USA"]}}})
    rules = countries.columns["country"]

    def offered(value):
        return [o["value"] for o in fixes.cell_options(value, f"'{value}' not in allowed values", rules, Counter())]

    assert offered("Scotland") == []
    assert offered("Italy") == []
    assert offered("U.K.") == ["UK"]
    assert offered("Frnace") == ["France"]


# ── Typed values, groups, rule changes and what's left ──

NIGHTS = DataSchema.model_validate({"name": "n", "columns": {
    "nights": {"dtype": "int", "nullable": False, "min_value": 1, "max_value": 30},
    "check_in": {"dtype": "date", "nullable": False, "date_format": "DMY"},
    "email": {"dtype": "str", "nullable": False, "pattern": ".+@.+\..+"},
    "room": {"dtype": "str", "nullable": False, "allowed_values": ["Single", "Double"]},
    "notes": {"dtype": "str", "nullable": True},
}})


@pytest.mark.parametrize("column, typed, expected", [
    ("nights", " 4 ", ("4", None)),
    ("nights", "45", ("", "That's above the maximum of 30.")),
    ("nights", "2.5", ("", "This column needs a whole number.")),
    ("nights", "four", ("", "That isn't a number.")),
    ("check_in", "25/03/2026", ("2026-03-25", None)),
    ("check_in", "03/25/2026", ("", "That isn't a date that can be read day first.")),
    ("email", "ana@example.com", ("ana@example.com", None)),
    ("email", "ana", ("", "That doesn't match the column's format.")),
    ("room", "Suite", ("", "That isn't one of the allowed values: Single, Double.")),
    ("email", "", ("", "This column can't be blank.")),
    ("notes", "", ("", None)),
])
def test_a_typed_value_is_checked_against_the_column_rules(column, typed, expected):
    assert fixes.check_value(typed, NIGHTS.columns[column]) == expected


CSV2 = (b"order_id,status,payment,price\n"
        b"1,Pending,Card,10.00\n"
        b"2,Returned,Card,20.00\n"
        b"3,Returned,PayPal,5000.00\n"
        b"4,Pending,Card,4000.00\n")
RUN_SCHEMA2 = json.dumps({"name": "o", "columns": {
    "order_id": {"dtype": "int"},
    "status": {"dtype": "str", "allowed_values": ["Pending", "Shipped"]},
    "payment": {"dtype": "str", "allowed_values": ["Card", "PayPal"]},
    "price": {"dtype": "float", "min_value": 0, "max_value": 3000},
}})


def run2(client):
    upload_id = client.post("/upload", files={"csv_file": ("o.csv", CSV2, "text/csv")}).json()["upload_id"]
    job_id = client.post("/clean", data={"upload_id": upload_id, "schema_json": RUN_SCHEMA2}).json()["job_id"]
    for _ in range(300):
        if web.store.get_job(job_id, None)["status"] not in UNFINISHED:
            return job_id
        time.sleep(0.01)
    raise AssertionError("the run didn't finish")


def test_typing_a_value_fixes_the_cell_and_a_bad_one_is_refused(client):
    job_id = run2(client)
    price = suggestion(client.get(f"/review/{job_id}").json(), "cell", column="price", row=4)

    refused = client.post(f"/api/jobs/{job_id}/decisions", json={"id": price["id"], "action": "edit", "value": "9000"})
    review = client.post(f"/api/jobs/{job_id}/decisions", json={"id": price["id"], "action": "edit", "value": "500"}).json()

    assert refused.status_code == 400
    assert "above the maximum" in refused.json()["detail"]
    assert review["rows"][2]["values"][3] == "500"
    assert review["rows"][2]["approved"] == {"price": "5000.0"}


def test_one_decision_for_a_whole_group(client):
    job_id = run2(client)
    returned = [s["id"] for s in client.get(f"/review/{job_id}").json()["suggestions"] if s.get("value") == "Returned"]

    review = client.post(f"/api/jobs/{job_id}/decisions/bulk", json={"ids": returned, "action": "keep"}).json()

    assert review["kept_cells"] == 2
    assert client.post(f"/api/jobs/{job_id}/decisions/bulk", json={"ids": returned, "action": "edit", "value": "x"}).status_code == 400
    assert client.post(f"/api/jobs/{job_id}/decisions/bulk", json={"ids": ["nope"], "action": "keep"}).status_code == 404


def test_allowing_a_value_clears_every_flag_it_caused_and_can_be_undone(client):
    job_id = run2(client)

    review = client.post(f"/api/jobs/{job_id}/decisions/rule",
                         json={"column": "status", "rule": "allow", "value": "Returned"}).json()

    assert review["kept_cells"] == 2
    assert review["rules"] == [{"id": "r1", "column": "status", "rule": "allow", "value": "Returned", "cells": 2}]
    undone = client.post(f"/api/jobs/{job_id}/decisions/rule/undo", json={"id": "r1"}).json()
    assert undone["kept_cells"] == 0 and undone["rules"] == []


def test_raising_a_maximum_only_clears_the_values_under_it(client):
    job_id = run2(client)

    review = client.post(f"/api/jobs/{job_id}/decisions/rule",
                         json={"column": "price", "rule": "max", "value": "4500"}).json()

    kept = [r["row"] for r in review["rows"] if "price" in r["kept"]]
    assert kept == [5]
    assert "price" in review["rows"][2]["issues"]
    assert client.post(f"/api/jobs/{job_id}/decisions/rule",
                       json={"column": "price", "rule": "max", "value": "100"}).status_code == 400


def test_the_still_to_fix_download_lists_only_rows_with_problems_left(client):
    job_id = run2(client)
    client.post(f"/api/jobs/{job_id}/decisions/rule", json={"column": "status", "rule": "allow", "value": "Returned"})

    lines = client.get(f"/download/{job_id}?file=remaining").text.splitlines()

    assert lines[0].startswith("row,order_id,")
    assert [l.split(",")[0] for l in lines[1:]] == ["4", "5"]
    assert all("Returned" not in l.split(",", 5)[-1] for l in lines[1:])
