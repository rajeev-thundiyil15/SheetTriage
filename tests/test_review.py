"""Tests for lining up the output with the original file, and the review route. No API calls."""
import threading
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app as web
import auth
import tools
from review import build_review
from storage import UNFINISHED, Store

ORIGINAL = b"id,dept,pay\n1,Enginering,100\n2,Sales,abc\n3,HR,300\n"


def log(row_index, column, error, value="abc"):
    return {"row_index": row_index, "row_id": str(row_index + 1), "column": column, "original_value": value, "error": error}


def write(tmp_path, **columns):
    path = tmp_path / "out.csv"
    pd.DataFrame(columns).to_csv(path, index=False)
    return str(path)


def test_row_numbers_match_a_spreadsheet_with_a_header(tmp_path):
    path = write(tmp_path, id=[1, 2, 3], dept=["Engineering", "Sales", "HR"], pay=["100", "abc", "300"], _errors=["", "bad pay", ""])

    review = build_review(ORIGINAL, path, [log(1, "pay", "bad pay")])

    assert [r["row"] for r in review["rows"]] == [2, 3, 4]
    assert review["report"][0]["row"] == 3


def test_errors_column_is_hidden_and_flagged_cells_are_marked(tmp_path):
    path = write(tmp_path, id=[1, 2, 3], dept=["Engineering", "Sales", "HR"], pay=["100", "abc", "300"], _errors=["", "bad pay", ""])

    review = build_review(ORIGINAL, path, [log(1, "pay", "bad pay")])

    assert review["columns"] == ["id", "dept", "pay"]
    assert review["rows"][1]["issues"] == {"pay": ["bad pay"]}
    assert review["rows"][0]["issues"] == {}
    assert review["flagged_rows"] == 1
    assert review["removed_flagged"] is False


def test_corrections_show_what_the_cell_was(tmp_path):
    path = write(tmp_path, id=[1, 2, 3], dept=["Engineering", "Sales", "HR"], pay=["100", "abc", "300"], _errors=["", "", ""])

    review = build_review(ORIGINAL, path, [])

    assert review["rows"][0]["changes"] == {"dept": "Enginering"}
    assert review["changed_cells"] == 1
    assert review["changed_rows"] == 1


def test_a_format_only_change_is_not_a_correction(tmp_path):
    path = write(tmp_path, id=["1.0", "2", "3"], dept=["Enginering", "Sales", "HR"], pay=["100.0", "abc", "300"], _errors=["", "", ""])

    assert build_review(ORIGINAL, path, [])["changed_cells"] == 0


def test_a_flagged_cell_never_counts_as_corrected(tmp_path):
    path = write(tmp_path, id=[1, 2, 3], dept=["Enginering", "Sales", "HR"], pay=["100", "", "300"], _errors=["", "bad", ""])

    review = build_review(ORIGINAL, path, [log(1, "pay", "bad")])

    assert review["rows"][1]["changes"] == {}
    assert review["rows"][1]["issues"] == {"pay": ["bad"]}
    assert review["changed_cells"] == 0


def test_strict_mode_keeps_rows_lined_up_after_removals(tmp_path):
    path = write(tmp_path, id=[1, 3], dept=["Engineering", "HR"], pay=["100", "300"])

    review = build_review(ORIGINAL, path, [log(1, "pay", "bad pay")])

    assert [r["row"] for r in review["rows"]] == [2, 4]
    assert review["rows"][1]["changes"] == {}
    assert review["rows"][0]["changes"] == {"dept": "Enginering"}
    assert review["removed_flagged"] is True
    assert review["rows_in_output"] == 2
    assert len(review["report"]) == 1


def test_rows_that_cannot_be_lined_up_are_shown_without_claims(tmp_path):
    path = write(tmp_path, id=[1], dept=["Sales"], pay=["1"])

    review = build_review(ORIGINAL, path, [])

    assert review["rows"][0]["row"] is None
    assert review["rows"][0]["changes"] == {}


def test_report_is_sorted_by_row(tmp_path):
    path = write(tmp_path, id=[1, 2, 3], dept=["a", "b", "c"], pay=["1", "2", "3"], _errors=["x", "x", ""])

    review = build_review(ORIGINAL, path, [log(1, "pay", "x"), log(0, "dept", "x")])

    assert [r["row"] for r in review["report"]] == [2, 3]


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "store", Store(tmp_path / "store"))
    monkeypatch.setattr(auth, "AUTH_MODE", "off")
    return TestClient(web.app)


def run_job(client, monkeypatch, write_output=True, finish=True):
    """Runs a job whose scripted agent saves this output and logs one flagged cell."""
    release = threading.Event()

    def scripted_agent(df, schema, output_path, mode, on_log):
        tools.set_dataframe(df)
        tools._state["error_log"].append(log(1, "pay", "bad pay"))
        if write_output:
            pd.DataFrame({"id": [1, 2, 3], "dept": ["Engineering", "Sales", "HR"], "pay": ["100", "abc", "300"],
                          "_errors": ["", "bad pay", ""]}).to_csv(output_path, index=False)
        if not finish:
            release.wait(5)
    monkeypatch.setattr(web, "run_agent", scripted_agent)

    upload_id = client.post("/upload", files={"csv_file": ("pay.csv", ORIGINAL, "text/csv")}).json()["upload_id"]
    schema = '{"name": "t", "columns": {"pay": {"dtype": "int"}}}'
    job_id = client.post("/clean", data={"upload_id": upload_id, "schema_json": schema}).json()["job_id"]
    if finish:
        for _ in range(200):
            if web.store.get_job(job_id, None)["status"] not in UNFINISHED:
                break
            time.sleep(0.01)
    return job_id, release


def test_review_route_returns_the_review_built_once(client, monkeypatch):
    job_id, _ = run_job(client, monkeypatch)

    first = client.get(f"/review/{job_id}")

    assert first.status_code == 200
    assert first.json()["flagged_rows"] == 1
    assert (web.store.job_dir(job_id) / "review.json").exists()
    assert web.store.get_job(job_id, None)["flagged_rows"] == 1
    assert client.get(f"/review/{job_id}").json() == first.json()


def test_review_route_for_an_unknown_job(client):
    assert client.get("/review/nope").status_code == 404


def test_review_route_while_running(client, monkeypatch):
    job_id, release = run_job(client, monkeypatch, finish=False)

    assert client.get(f"/review/{job_id}").status_code == 409
    release.set()


def test_review_route_when_nothing_was_saved(client, monkeypatch):
    job_id, _ = run_job(client, monkeypatch, write_output=False)

    assert client.get(f"/review/{job_id}").status_code == 404


@pytest.mark.parametrize("before, after, kind", [
    ("02/14/2026", "2026-02-14", "reformatted"),
    ("14/02/2026", "2026-02-14", "reformatted"),
    ("March 5 2026", "2026-03-05", "reformatted"),
    ("2026-03-15", "2026-03-15", None),
    ("45.00", "45.0", "reformatted"),
    ("vip", "VIP", "corrected"),
    ("", "Sales", "corrected"),
    ("02/14/2026", "2026-02-15", "corrected"),
    ("Enginering", "Engineering", "corrected"),
])
def test_format_changes_are_told_apart_from_corrections(before, after, kind):
    from review import _kind_of_change

    assert _kind_of_change(before, after) == kind


def test_reformatted_dates_are_counted_but_not_marked_as_corrections(tmp_path):
    # Story 49: the progress line said 0 fixed while the review said 29 cells corrected,
    # all of them dates written as 2024-02-14.
    original = b"id,signed,dept\n1,02/14/2024,Enginering\n2,03/01/2024,Sales\n"
    path = write(tmp_path, id=[1, 2], signed=["2024-02-14", "2024-03-01"], dept=["Engineering", "Sales"], _errors=["", ""])

    review = build_review(original, path, [])

    assert review["changed_cells"] == 1
    assert review["changed_rows"] == 1
    assert review["reformatted_cells"] == 2
    assert review["rows"][0]["changes"] == {"dept": "Enginering"}
    assert review["rows"][1]["changes"] == {}
