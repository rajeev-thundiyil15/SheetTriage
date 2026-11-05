"""Tests for the pandas tools Claude calls. No API calls: everything here is deterministic."""
import csv
import json

import pandas as pd
import pytest

import tools as t
from schema import ColumnSchema, DataSchema


def load(**columns) -> None:
    t.set_dataframe(pd.DataFrame(columns))


def schema_of(**columns) -> DataSchema:
    return DataSchema(name="test", columns={k: ColumnSchema(**v) for k, v in columns.items()})


def validate(column: str, schema: DataSchema) -> dict:
    return json.loads(t.validate_column(column, schema))


def save(tmp_path, schema=None, **kwargs) -> dict:
    return json.loads(
        t.save_with_report(str(tmp_path / "out.csv"), str(tmp_path / "report.csv"), schema=schema, **kwargs)
    )


# ---------------------------------------------------------------------------
# validate_column
# ---------------------------------------------------------------------------

def test_validate_groups_rows_by_issue_text():
    load(price=["abc", "abc", "1.5", "xyz"])
    result = validate("price", schema_of(price={"dtype": "float"}))

    assert result["total_violations"] == 3
    assert result["distinct_issue_groups"] == 2
    top = result["groups"][0]
    assert top["issue"] == "cannot convert 'abc' to float"
    assert top["count"] == 2
    assert top["rows"] == [0, 1]


def test_validate_caps_groups_at_40_and_adds_note():
    load(price=[f"bad{i}" for i in range(45)])
    result = validate("price", schema_of(price={"dtype": "float"}))

    assert result["total_violations"] == 45
    assert result["distinct_issue_groups"] == 45
    assert len(result["groups"]) == 40
    assert "40 most common of 45" in result["note"]


def test_validate_shows_no_note_at_exactly_40_groups():
    load(price=[f"bad{i}" for i in range(40)])
    result = validate("price", schema_of(price={"dtype": "float"}))

    assert len(result["groups"]) == 40
    assert "note" not in result


def test_validate_skips_rows_already_marked_for_that_column():
    load(price=["abc", "xyz"])
    schema = schema_of(price={"dtype": "float"})
    t.mark_row_error(0, "price", "unparseable")

    result = validate("price", schema)

    assert result["total_violations"] == 1
    assert result["groups"][0]["rows"] == [1]


def test_validate_still_checks_a_row_marked_on_a_different_column():
    load(price=["abc"], name=["x"])
    t.mark_row_error(0, "name", "suspicious")

    assert validate("price", schema_of(price={"dtype": "float"}))["total_violations"] == 1


def test_validate_null_rules():
    load(required=[None, "a"], optional=[None, "a"])
    schema = schema_of(required={"dtype": "str"}, optional={"dtype": "str", "nullable": True})

    assert validate("required", schema)["groups"][0]["issue"] == "null value not allowed"
    assert validate("optional", schema)["total_violations"] == 0


def test_validate_range_allowed_values_and_pattern():
    load(age=[17, 50, 101], dept=["HR", "Hr", "HR"], email=["a@b.co", "nope", "c@d.io"])
    schema = schema_of(
        age={"dtype": "int", "min_value": 18, "max_value": 100},
        dept={"dtype": "str", "allowed_values": ["HR"]},
        email={"dtype": "str", "pattern": r".+@.+\..+"},
    )

    assert sorted(g["issue"] for g in validate("age", schema)["groups"]) == [
        "value 101 is above max 100.0",
        "value 17 is below min 18.0",
    ]
    assert validate("dept", schema)["groups"][0]["rows"] == [1]
    assert validate("email", schema)["groups"][0]["rows"] == [1]


def test_validate_reports_unknown_columns_as_errors():
    load(a=[1])
    schema = schema_of(a={"dtype": "int"}, missing={"dtype": "int"})

    assert "not in DataFrame" in validate("missing", schema)["error"]
    assert "not in schema" in validate("a", schema_of(b={"dtype": "int"}))["error"]


def test_validate_rejects_a_fractional_value_in_an_int_column():
    load(qty=["3.7"])
    assert validate("qty", schema_of(qty={"dtype": "int"}))["total_violations"] == 1


# ---------------------------------------------------------------------------
# fix_dtype / fix_allowed_values / fill_null
# ---------------------------------------------------------------------------

def test_fix_dtype_counts_conversions_and_failures():
    load(price=["1.5", "abc", None])
    result = json.loads(t.fix_dtype("price", "float"))

    assert result["successfully_converted"] == 1
    assert result["failed_to_convert"] == 1
    assert t.get_dataframe()["price"].iloc[0] == 1.5


def test_fix_dtype_converts_whole_numbers_to_int():
    load(qty=["1", "2.0"])
    result = json.loads(t.fix_dtype("qty", "int"))

    assert result["successfully_converted"] == 2
    assert t.get_dataframe()["qty"].tolist() == [1, 2]


def test_fix_dtype_int_reports_a_fractional_value_as_a_failure():
    load(qty=["3.7", "1"])
    assert json.loads(t.fix_dtype("qty", "int"))["failed_to_convert"] == 1


def test_fix_dtype_date_keeps_iso_month_and_day():
    load(when=["2024-11-03", "2024-01-15"])
    t.fix_dtype("when", "date")

    assert t.get_dataframe()["when"].dt.strftime("%Y-%m-%d").tolist() == ["2024-11-03", "2024-01-15"]


def test_fix_dtype_date_handles_mixed_formats():
    load(when=["2024-11-03", "15/01/2024"])
    assert json.loads(t.fix_dtype("when", "date"))["failed_to_convert"] == 0


def test_fix_dtype_failure_in_a_nullable_column_is_not_silently_erased():
    load(rating=["4.5", "great"])
    schema = schema_of(rating={"dtype": "float", "nullable": True})

    t.fix_dtype("rating", "float")

    # Either the bad value survives or validation still reports it. Today it is a clean NaN.
    survived = "great" in t.get_dataframe()["rating"].astype(str).tolist()
    assert survived or validate("rating", schema)["total_violations"] == 1


def test_report_keeps_the_original_value_after_a_failed_conversion():
    load(price=["abc"])
    t.fix_dtype("price", "float")
    t.mark_row_error(0, "price", "could not convert")

    assert t._state["error_log"][0]["original_value"] == "abc"


def test_fix_dtype_str_leaves_nulls_null():
    load(name=["a", None])
    t.fix_dtype("name", "str")

    assert t.get_dataframe()["name"].isna().tolist() == [False, True]


def test_fix_allowed_values_changes_only_matching_cells():
    load(dept=["Hr", "HR", "Sales", "Hr"])
    result = json.loads(t.fix_allowed_values("dept", {"Hr": "HR", "Unused": "X"}))

    assert result["total_cells_changed"] == 2
    assert t.get_dataframe()["dept"].tolist() == ["HR", "HR", "Sales", "HR"]


def test_fill_null_changes_only_nulls():
    load(dept=[None, "HR", None])
    result = json.loads(t.fill_null("dept", "Sales"))

    assert result["affected"] == 2
    assert t.get_dataframe()["dept"].tolist() == ["Sales", "HR", "Sales"]


def test_fill_null_with_no_nulls_changes_nothing():
    load(dept=["HR"])

    assert json.loads(t.fill_null("dept", "Sales"))["affected"] == 0
    assert t.get_dataframe()["dept"].tolist() == ["HR"]


# ---------------------------------------------------------------------------
# mark_row_error / bulk_mark_errors
# ---------------------------------------------------------------------------

def test_bulk_mark_keeps_every_row_and_fills_errors_column():
    load(id=[1, 2, 3], price=["a", "1", "b"])
    result = json.loads(t.bulk_mark_errors([
        {"row_index": 0, "column_name": "price", "issue": "bad price"},
        {"row_index": 2, "column_name": "price", "issue": "bad price"},
    ]))
    df = t.get_dataframe()

    assert result["marked"] == 2
    assert len(df) == 3
    assert df["_errors"].tolist() == ["bad price", "", "bad price"]


def test_errors_column_joins_multiple_issues_on_one_row():
    load(price=["a"], name=[None])
    t.mark_row_error(0, "price", "bad price")
    t.mark_row_error(0, "name", "missing name")

    assert t.get_dataframe()["_errors"].iloc[0] == "bad price; missing name"


def test_mark_row_error_logs_the_row_id_and_current_value():
    load(sku=[101, 102], price=["1", "abc"])
    t.mark_row_error(1, "price", "bad price", id_column="sku")

    assert t._state["error_log"] == [{
        "row_index": 1,
        "row_id": "102",
        "column": "price",
        "original_value": "abc",
        "error": "bad price",
    }]


def test_a_blank_cell_is_reported_as_blank():
    load(name=[None])
    t.mark_row_error(0, "name", "missing name")

    assert t._state["error_log"][0]["original_value"] == "(blank)"


def test_mark_row_error_rejects_an_unknown_row():
    load(price=["1"])
    assert "not in DataFrame" in json.loads(t.mark_row_error(99, "price", "bad"))["error"]


def test_bulk_mark_does_not_count_an_unknown_row_as_marked():
    load(price=["1"])
    assert json.loads(t.bulk_mark_errors([{"row_index": 99, "column_name": "price", "issue": "bad"}]))["marked"] == 0


def test_mark_remaining_handles_thousands_of_rows_and_keeps_them(tmp_path):
    load(a=["x"] * 3000 + ["1"])
    schema = schema_of(a={"dtype": "int"})

    result = json.loads(t.mark_remaining_violations("a", schema))

    assert result["marked"] == 3000
    assert len(t.get_dataframe()) == 3001
    assert validate("a", schema)["total_violations"] == 0
    assert save(tmp_path, schema)["rows_with_errors"] == 3000


def test_mark_remaining_logs_the_validator_issue_text():
    load(a=["x"])
    t.mark_remaining_violations("a", schema_of(a={"dtype": "int"}))

    assert t._state["error_log"][0]["error"] == "cannot convert 'x' to int"
    assert t._state["error_log"][0]["original_value"] == "x"


def test_mark_remaining_only_issue_leaves_other_groups():
    load(a=["x", "y"])
    schema = schema_of(a={"dtype": "int"})

    result = json.loads(t.mark_remaining_violations("a", schema, only_issue="cannot convert 'x' to int"))

    assert result["marked"] == 1
    assert validate("a", schema)["groups"][0]["rows"] == [1]


def test_mark_remaining_rejects_unknown_columns():
    load(a=["x"])
    assert "error" in json.loads(t.mark_remaining_violations("b", schema_of(a={"dtype": "int"})))


# ---------------------------------------------------------------------------
# save_with_report: the completion gate
# ---------------------------------------------------------------------------

def test_gate_refuses_when_a_column_was_never_validated(tmp_path):
    load(a=[1], b=[2])
    schema = schema_of(a={"dtype": "int"}, b={"dtype": "int"})
    validate("a", schema)

    result = save(tmp_path, schema)

    assert "b" in result["error"]
    assert not (tmp_path / "out.csv").exists()


def test_gate_refuses_when_the_last_validation_found_violations(tmp_path):
    load(a=["x"])
    schema = schema_of(a={"dtype": "int"})
    validate("a", schema)

    assert "a" in save(tmp_path, schema)["error"]
    assert not (tmp_path / "out.csv").exists()


def test_gate_passes_once_every_column_validates_clean(tmp_path):
    load(a=[1], b=[2])
    schema = schema_of(a={"dtype": "int"}, b={"dtype": "int"})
    validate("a", schema)
    validate("b", schema)

    assert save(tmp_path, schema)["rows_in_output_csv"] == 1


def test_gate_closes_again_when_a_later_validation_finds_violations(tmp_path):
    load(a=[1])
    schema = schema_of(a={"dtype": "int"})
    validate("a", schema)
    t.get_dataframe().loc[0, "a"] = None  # Column changed after the clean result.
    validate("a", schema)

    assert "error" in save(tmp_path, schema)


def test_gate_passes_after_violations_are_marked_and_revalidated(tmp_path):
    load(a=["x", "1"])
    schema = schema_of(a={"dtype": "int"})
    validate("a", schema)
    t.mark_row_error(0, "a", "bad")
    validate("a", schema)

    result = save(tmp_path, schema)

    assert result["rows_with_errors"] == 1
    assert result["clean_rows"] == 1


def test_gate_state_resets_for_a_new_file(tmp_path):
    load(a=[1])
    schema = schema_of(a={"dtype": "int"})
    validate("a", schema)
    load(a=["x"])

    assert "error" in save(tmp_path, schema)


@pytest.mark.parametrize("change", [
    lambda: t.fill_null("a", "not a number"),
    lambda: t.fix_allowed_values("a", {"1": "not a number"}),
    lambda: t.fix_dtype("a", "str"),
])
def test_gate_requires_a_fresh_validation_after_a_fix(tmp_path, change):
    load(a=["1", None])
    schema = schema_of(a={"dtype": "int", "nullable": True})
    validate("a", schema)

    change()

    assert "error" in save(tmp_path, schema)


# ---------------------------------------------------------------------------
# save_with_report: output files
# ---------------------------------------------------------------------------

def test_save_keeps_error_rows_by_default(tmp_path):
    load(a=[1, 2])
    t.mark_row_error(0, "a", "bad")

    result = save(tmp_path)
    out = pd.read_csv(tmp_path / "out.csv")

    assert result["rows_in_output_csv"] == 2
    assert "_errors" in out.columns


def test_strict_save_drops_error_rows_from_csv_but_not_report(tmp_path):
    load(a=[1, 2])
    t.mark_row_error(0, "a", "bad")

    result = save(tmp_path, exclude_error_rows=True)
    out = pd.read_csv(tmp_path / "out.csv")

    assert out["a"].tolist() == [2]
    assert "_errors" not in out.columns
    assert result["total_input_rows"] == 2
    assert result["total_issues_logged"] == 1
    assert "bad" in (tmp_path / "report.csv").read_text(encoding="utf-8")


def test_breakdown_matches_the_error_log(tmp_path):
    load(a=[1, 2, 3], b=[1, 2, 3])
    t.bulk_mark_errors([
        {"row_index": 0, "column_name": "a", "issue": "x"},
        {"row_index": 1, "column_name": "a", "issue": "x"},
        {"row_index": 2, "column_name": "a", "issue": "y"},
        {"row_index": 0, "column_name": "b", "issue": "z"},
    ])

    result = save(tmp_path)

    assert result["total_issues_logged"] == 4
    assert result["issues_by_column"] == {"a": 3, "b": 1}
    assert result["top_issues"][0] == {"column": "a", "issue": "x", "count": 2}
    assert sum(i["count"] for i in result["top_issues"]) == 4
    assert result["rows_with_errors"] == 3


def test_the_same_problem_on_different_values_counts_as_one_issue(tmp_path):
    load(a=["x", "O'Brien", "z"])
    t.bulk_mark_errors([
        {"row_index": 0, "column_name": "a", "issue": "cannot convert 'x' to int"},
        {"row_index": 1, "column_name": "a", "issue": "cannot convert 'O'Brien' to int"},
        {"row_index": 2, "column_name": "a", "issue": "cannot convert 'z' to int"},
    ])

    result = save(tmp_path)

    assert result["top_issues"] == [{"column": "a", "issue": "cannot convert a value to int", "count": 3}]


def test_top_issues_lists_at_most_15(tmp_path):
    load(a=list(range(20)))
    t.bulk_mark_errors([{"row_index": i, "column_name": "a", "issue": f"issue {i}"} for i in range(20)])

    result = save(tmp_path)

    assert len(result["top_issues"]) == 15
    assert result["total_issues_logged"] == 20


def test_report_summary_and_details(tmp_path):
    load(sku=[7], a=["x"])
    t.mark_row_error(0, "a", "bad value", id_column="sku")
    save(tmp_path)

    rows = list(csv.reader((tmp_path / "report.csv").open(encoding="utf-8")))

    assert rows[0] == ["SUMMARY"]
    assert ["a", "1"] in rows
    assert ["total_errors", "1"] in rows
    assert ["rows_affected", "1"] in rows
    assert ["7", "a", "x", "bad value", "Review & correct manually"] in rows


def test_report_for_a_clean_file(tmp_path):
    load(a=[1])
    save(tmp_path)

    assert (tmp_path / "report.csv").read_text(encoding="utf-8").strip() == "No errors found."
