"""Tests for lining up the output with the original file, and the review route. No API calls."""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app as web
from review import build_review

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
def client():
    web._jobs.clear()
    return TestClient(web.app)


def job(path, done=True):
    return {"output_path": path, "done": done, "created_at": 0, "contents": ORIGINAL, "issues": [log(1, "pay", "bad pay")]}


def test_review_route_returns_the_review_and_caches_it(client, tmp_path):
    path = write(tmp_path, id=[1, 2, 3], dept=["Engineering", "Sales", "HR"], pay=["100", "abc", "300"], _errors=["", "bad pay", ""])
    web._jobs["j"] = job(path)

    first = client.get("/review/j")

    assert first.status_code == 200
    assert first.json()["flagged_rows"] == 1
    assert web._jobs["j"]["contents"] is None
    assert client.get("/review/j").json() == first.json()


@pytest.mark.parametrize("setup, expected", [
    (None, 404),
    ({"output_path": "missing.csv", "done": False, "created_at": 0}, 409),
    ({"output_path": "missing.csv", "done": True, "created_at": 0}, 404),
])
def test_review_route_errors(client, setup, expected):
    if setup:
        web._jobs["j"] = setup

    assert client.get("/review/j").status_code == expected
