"""
tools.py — pandas operations exposed to Claude as tools.

Design principle: never silently delete data.
- Fixable violations (typos, dtype coercion, date formats) → fix in place.
- Unfixable violations (can't infer correct value) → mark_row_error, keep the row.
- save_with_report writes two files: annotated CSV (_errors column) + report CSV.
"""
from __future__ import annotations

import csv
import re
import json
import pandas as pd
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from schema import DataSchema


NUMERIC_DATE = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4}|\d{2})(?:\s.*)?$")
YEAR_FIRST_DATE = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T].*)?$")

_state: dict[str, Any] = {
    "df": None,
    "error_log": [],        # [{row_index, id_value, column, original_value, error}]
    "validated_clean": set(),  # columns confirmed 0 violations as of their last validate_column call
}


def set_dataframe(df: pd.DataFrame) -> None:
    _state["df"] = df.copy()
    _state["error_log"] = []
    _state["validated_clean"] = set()


def get_dataframe() -> pd.DataFrame:
    return _state["df"]


def get_error_log() -> list[dict]:
    return list(_state["error_log"])


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------

def inspect_data() -> str:
    df: pd.DataFrame = _state["df"]
    info = {
        "shape": {"rows": len(df), "columns": len(df.columns)},
        "columns": list(df.columns),
        "dtypes": {col: str(df[col].dtype) for col in df.columns},
        "null_counts": {col: int(df[col].isna().sum()) for col in df.columns},
        "sample_rows": df.head(5).to_dict(orient="records"),
    }
    return json.dumps(info, indent=2, default=str)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

MAX_GROUPS_SHOWN = 40
MAX_EXAMPLE_ROWS = 5
MAX_GROUPS_CHARS = 3200  # leaves room for the rest of the result under the agent's 4,000


def validate_column(column_name: str, schema: DataSchema) -> str:
    """
    Returns violations grouped by the exact issue text, each with its true count and a
    few example row indices — e.g. one group for "cannot convert 'sixty five' to float"
    covering 40 rows, instead of 40 near-identical entries.

    The agent only sees the first 4,000 characters of a result, so the output is kept
    under that whatever the file size: full row lists on a 30,000-row file once pushed
    smaller groups past the cut-off, and the agent never saw them. Rows are marked on
    the server by mark_remaining_violations, so the agent never needs the full list.
    """
    df: pd.DataFrame = _state["df"]

    if column_name not in df.columns:
        return json.dumps({"error": f"Column '{column_name}' not in DataFrame"})
    if column_name not in schema.columns:
        return json.dumps({"error": f"Column '{column_name}' not in schema"})

    flat = _find_violations(column_name, schema)

    if not flat:
        _state["validated_clean"].add(column_name)
        return json.dumps({"column": column_name, "total_violations": 0, "groups": []})

    _state["validated_clean"].discard(column_name)

    groups: dict[str, dict] = {}
    for row_idx, issue in flat:
        g = groups.setdefault(issue, {"issue": issue, "count": 0, "example_rows": []})
        g["count"] += 1
        if len(g["example_rows"]) < MAX_EXAMPLE_ROWS:
            g["example_rows"].append(row_idx)

    group_list = sorted(groups.values(), key=lambda g: -g["count"])

    shown: list[dict] = []
    size = 0
    for g in group_list[:MAX_GROUPS_SHOWN]:
        g_size = len(json.dumps(g)) + 2
        if shown and size + g_size > MAX_GROUPS_CHARS:
            break
        shown.append(g)
        size += g_size

    result = {
        "column": column_name,
        "total_violations": len(flat),
        "distinct_issue_groups": len(group_list),
        "groups": shown,
    }
    if len(shown) < len(group_list):
        result["note"] = (
            f"Showing the {len(shown)} most common of {len(group_list)} distinct issue "
            "groups. After fixing or marking these, call validate_column again on this column — "
            "already-handled rows are skipped automatically, so the next-most-common remaining "
            "groups will surface."
        )

    return json.dumps(result)


# ---------------------------------------------------------------------------
# Marking — preferred over dropping for unfixable violations
# ---------------------------------------------------------------------------

def mark_row_error(row_index: int, column_name: str, issue: str, id_column: str = "") -> str:
    """
    Records a violation on a row without removing it.
    - Appends to the row's _errors cell so the human can see it in the output CSV.
    - Logs the violation for the report CSV.
    Use this whenever a violation cannot be safely auto-corrected.
    """
    df: pd.DataFrame = _state["df"]

    if row_index not in df.index:
        return json.dumps({"error": f"Row index {row_index} not in DataFrame"})

    # Ensure _errors column exists
    if "_errors" not in df.columns:
        df["_errors"] = ""

    # Append to the row's error string
    current = str(df.at[row_index, "_errors"]) if df.at[row_index, "_errors"] else ""
    df.at[row_index, "_errors"] = (current + "; " + issue).lstrip("; ")

    # Capture the original (current) value for the report
    if column_name not in df.columns:
        original_value = "(unknown)"
    elif pd.isna(df.at[row_index, column_name]):
        original_value = "(blank)"
    else:
        original_value = str(df.at[row_index, column_name])

    # Resolve a human-readable row identifier
    id_val = _resolve_id(df, row_index, id_column)

    _state["error_log"].append({
        "row_index": row_index,
        "row_id": id_val,
        "column": column_name,
        "original_value": original_value,
        "error": issue,
    })

    return json.dumps({
        "marked_row": row_index,
        "row_id": id_val,
        "column": column_name,
        "issue": issue,
        "note": "Row kept in dataset — flagged in _errors column and report.",
    })


# ---------------------------------------------------------------------------
# Bulk marking — use this for large files instead of mark_row_error N times
# ---------------------------------------------------------------------------

def bulk_mark_errors(violations: list[dict]) -> str:
    """
    Mark multiple rows in one call.
    violations: list of {row_index, column_name, issue, id_column (optional)}
    """
    marked, errors = 0, []
    for v in violations:
        try:
            outcome = json.loads(mark_row_error(
                int(v["row_index"]),
                v["column_name"],
                v["issue"],
                v.get("id_column", ""),
            ))
            if "error" in outcome:
                errors.append(outcome["error"])
            else:
                marked += 1
        except Exception as exc:
            errors.append(str(exc))
    result = {"marked": marked}
    if errors:
        result["errors"] = errors[:5]
    return json.dumps(result)


def mark_remaining_violations(column_name: str, schema: DataSchema, only_issue: str = "", id_column: str = "") -> str:
    """
    Marks every row that still violates the schema in this column, working the rows out
    here rather than asking Claude to list them. A tool call can't hold thousands of row
    indices, so this is what keeps large files saveable.
    only_issue limits marking to one validate_column group, matched on its exact 'issue' text.
    """
    df: pd.DataFrame = _state["df"]

    if column_name not in df.columns:
        return json.dumps({"error": f"Column '{column_name}' not in DataFrame"})
    if column_name not in schema.columns:
        return json.dumps({"error": f"Column '{column_name}' not in schema"})

    flat = _find_violations(column_name, schema)
    if only_issue:
        flat = [(row, issue) for row, issue in flat if issue == only_issue]

    for row_index, issue in flat:
        mark_row_error(row_index, column_name, issue, id_column)

    return json.dumps({"column": column_name, "marked": len(flat), "rows": len({row for row, _ in flat})})


# ---------------------------------------------------------------------------
# Fixes (for violations that CAN be auto-corrected)
# ---------------------------------------------------------------------------

def fix_dtype(column_name: str, target_dtype: str, date_format: str | None = None) -> str:
    """
    Coerce a column to the target dtype. Values that fail keep their original text, so
    validate_column still reports them and the report shows what the user actually had.
    """
    df: pd.DataFrame = _state["df"]
    original = df[column_name].copy()
    _state["validated_clean"].discard(column_name)

    parsed = pd.Series(
        [None if pd.isna(v) else _try_coerce(v, target_dtype, date_format) for v in original],
        index=original.index,
        dtype=object,
    )
    failed_mask = original.notna() & parsed.isna()
    failed = int(failed_mask.sum())
    converted = int(parsed.notna().sum())

    if failed:
        if target_dtype == "date":
            parsed = parsed.map(lambda v: v.strftime("%Y-%m-%d") if isinstance(v, pd.Timestamp) else v)
        df[column_name] = parsed.where(~failed_mask, original.astype(object))
    elif target_dtype == "int":
        df[column_name] = pd.array(parsed.where(parsed.notna(), None).tolist(), dtype="Int64")
    elif target_dtype == "float":
        df[column_name] = pd.to_numeric(parsed)
    elif target_dtype == "date":
        df[column_name] = pd.to_datetime(parsed)
    elif target_dtype == "str":
        df[column_name] = parsed

    return json.dumps({
        "column": column_name,
        "dtype_applied": target_dtype,
        "successfully_converted": converted,
        "failed_to_convert": failed,
        "note": (
            f"{failed} value(s) could not be converted and were left unchanged. "
            "Call validate_column again, then mark_remaining_violations for this column."
        ) if failed else "",
    })


TYPO_SIMILARITY = 0.8  # 'Studnet'/'Student' scores 0.86; 'U.S.'/'USA' 0.57, 'Early Bird'/'General' 0.35


def _is_typo_of(bad: str, good: str) -> bool:
    """True when bad is good with different casing or spacing, or a close misspelling of it."""
    a, b = " ".join(str(bad).casefold().split()), " ".join(str(good).casefold().split())
    return a == b or SequenceMatcher(None, a, b).ratio() >= TYPO_SIMILARITY


def fix_allowed_values(
    column_name: str,
    replacements: dict[str, str],
    allowed_values: list | None = None,
) -> str:
    """
    Replace disallowed values with correct ones (typos, casing, etc.).

    With allowed_values given, a replacement only goes through when the new value is allowed
    and the old one is a casing change or close misspelling of it. Anything else is a guess,
    so it is refused and the value stays as written, to be flagged.
    """
    df: pd.DataFrame = _state["df"]
    _state["validated_clean"].discard(column_name)
    applied: dict[str, str] = {}
    refused: dict[str, str] = {}
    total_replaced = 0
    for bad, good in replacements.items():
        if allowed_values is not None:
            if good not in allowed_values:
                refused[bad] = f"'{good}' is not an allowed value"
                continue
            if not _is_typo_of(bad, good):
                refused[bad] = f"'{bad}' is not a typo or casing change of '{good}'"
                continue
        applied[bad] = good
        mask = df[column_name] == bad
        count = int(mask.sum())
        if count:
            df.loc[mask, column_name] = good
            total_replaced += count

    result = {
        "column": column_name,
        "replacements_applied": applied,
        "total_cells_changed": total_replaced,
    }
    if refused:
        result["refused"] = refused
        result["note"] = "Refused values were left unchanged. Flag them with mark_remaining_violations."
    return json.dumps(result)


def fill_null(column_name: str, fill_value: str) -> str:
    """Fill null values with a known correct value (only use when the value is certain)."""
    df: pd.DataFrame = _state["df"]
    _state["validated_clean"].discard(column_name)
    null_mask = df[column_name].isna()
    count = int(null_mask.sum())
    if count == 0:
        return json.dumps({"message": "No nulls found", "affected": 0})
    df.loc[null_mask, column_name] = fill_value
    return json.dumps({"message": f"Filled {count} nulls in '{column_name}' with '{fill_value}'", "affected": count})


# ---------------------------------------------------------------------------
# Save — always produces annotated CSV + report
# ---------------------------------------------------------------------------

def save_with_report(
    csv_path: str,
    report_path: str,
    id_column: str = "",
    exclude_error_rows: bool = False,
    schema: DataSchema | None = None,
) -> str:
    """
    Writes two files:
    1. CSV output — all rows (or only clean rows when exclude_error_rows=True), _errors column added.
    2. Report CSV — one row per violation: who, which field, what's wrong.

    exclude_error_rows=True → strict mode: only rows with no errors are saved to the CSV.

    Refuses to save until every schema column has a fresh validate_column call showing
    0 violations — prevents an early/incomplete save from being reported as "all clean".
    """
    df: pd.DataFrame = _state["df"]

    if schema is not None:
        missing = [c for c in schema.columns if c not in _state["validated_clean"]]
        if missing:
            return json.dumps({
                "error": (
                    "Not all columns confirmed clean yet. Still need a validate_column "
                    f"call (showing 0 remaining violations) for: {', '.join(missing)}. "
                    "Fix or flag anything it finds, then validate_column again until it "
                    "reports 0, then retry save_with_report."
                )
            })

    if "_errors" not in df.columns:
        df["_errors"] = ""
    df["_errors"] = df["_errors"].fillna("")

    clean_rows  = int((df["_errors"] == "").sum())
    flagged_rows = int((df["_errors"] != "").sum())

    # In strict mode, drop error rows from the output CSV
    output_df = df[df["_errors"] == ""].drop(columns=["_errors"]) if exclude_error_rows else df

    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    output_df.to_csv(csv_path, index=False)

    # Report is always the full error log regardless of mode
    log = _state["error_log"]
    Path(report_path).parent.mkdir(parents=True, exist_ok=True)

    col_counts = Counter(e["column"] for e in log)
    # Leave the cell's own value out, so the same problem on different values counts as one issue.
    issue_counts = Counter(
        (e["column"], e["error"].replace(f"'{e['original_value']}'", "a value", 1)) for e in log
    )
    top_issues = [
        {"column": col, "issue": issue, "count": count}
        for (col, issue), count in issue_counts.most_common(15)
    ]

    with open(report_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)

        if log:
            # ── Summary block ──
            affected_rows = len(set(e["row_index"] for e in log))

            w.writerow(["SUMMARY"])
            w.writerow(["column", "error_count"])
            for col, count in sorted(col_counts.items(), key=lambda x: -x[1]):
                w.writerow([col, count])
            w.writerow([])
            w.writerow(["total_errors", len(log)])
            w.writerow(["rows_affected", affected_rows])
            w.writerow([])

            # ── Detail block ──
            w.writerow(["DETAILS"])
            w.writerow(["row_id", "column", "original_value", "error", "action_needed"])
            for e in log:
                w.writerow([e["row_id"], e["column"], e["original_value"], e["error"], "Review & correct manually"])
        else:
            w.writerow(["No errors found."])

    return json.dumps({
        "csv_saved": csv_path,
        "report_saved": report_path,
        "total_input_rows": len(df),
        "clean_rows": clean_rows,
        "rows_with_errors": flagged_rows,
        "rows_in_output_csv": len(output_df),
        "total_issues_logged": len(log),
        "issues_by_column": dict(col_counts),
        "top_issues": top_issues,
    })


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _find_violations(column_name: str, schema: DataSchema) -> list[tuple[int, str]]:
    """(row_index, issue_text) for every unmarked violation in a column."""
    df: pd.DataFrame = _state["df"]
    col_schema = schema.columns[column_name]
    flat: list[tuple[int, str]] = []  # (row_index, issue_text)

    for idx, val in df[column_name].items():
        # Skip rows already marked for this column
        if _already_marked(idx, column_name):
            continue

        if pd.isna(val):
            if not col_schema.nullable:
                flat.append((int(idx), "null value not allowed"))
            continue

        coerced = _try_coerce(val, col_schema.dtype, col_schema.date_format)
        if coerced is None and col_schema.dtype == "date" and not col_schema.date_format and _is_ambiguous_date(val):
            flat.append((int(idx), f"date '{val}' could be day first or month first; no date format was chosen"))
        elif coerced is None:
            flat.append((int(idx), f"cannot convert '{val}' to {col_schema.dtype}"))

        if coerced is not None and col_schema.dtype in ("int", "float"):
            if col_schema.min_value is not None and coerced < col_schema.min_value:
                flat.append((int(idx), f"value {coerced} is below min {col_schema.min_value}"))
            if col_schema.max_value is not None and coerced > col_schema.max_value:
                flat.append((int(idx), f"value {coerced} is above max {col_schema.max_value}"))

        if col_schema.allowed_values and str(val) not in col_schema.allowed_values:
            flat.append((int(idx), f"'{val}' not in allowed values {col_schema.allowed_values}"))

        if col_schema.pattern and not re.fullmatch(col_schema.pattern, str(val)):
            flat.append((int(idx), f"'{val}' does not match pattern {col_schema.pattern}"))

    return flat


def _is_ambiguous_date(value: Any) -> bool:
    match = NUMERIC_DATE.match(str(value).strip())
    if not match:
        return False
    first, second = int(match.group(1)), int(match.group(2))
    return first != second and first <= 12 and second <= 12


def _parse_date(value: Any, date_format: str | None = None) -> pd.Timestamp:
    """
    Year-first and named-month dates can't be misread. A date like 03/04/2024 can, so it
    follows date_format, and with none chosen only an unambiguous one is accepted.
    """
    text = str(value).strip()
    if YEAR_FIRST_DATE.match(text):
        return pd.to_datetime(text)

    match = NUMERIC_DATE.match(text)
    if not match:
        return pd.to_datetime(text)

    first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    year = year + 2000 if year < 100 else year
    if date_format == "YMD" or (date_format is None and _is_ambiguous_date(text)):
        raise ValueError(f"cannot read {text} with date format {date_format}")
    day_first = date_format == "DMY" or (date_format is None and first > 12)
    return pd.Timestamp(year=year, month=second if day_first else first, day=first if day_first else second)


def _try_coerce(value: Any, dtype: str, date_format: str | None = None) -> Any:
    try:
        if dtype == "int":
            number = float(str(value))
            return int(number) if number == int(number) else None
        if dtype == "float": return float(str(value))
        if dtype == "str":   return str(value)
        if dtype == "date":  return _parse_date(value, date_format)
    except Exception:
        return None
    return None


def _resolve_id(df: pd.DataFrame, row_index: int, id_column: str) -> str:
    """Returns a human-readable identifier for a row."""
    if id_column and id_column in df.columns:
        return str(df.at[row_index, id_column])
    # Auto-detect: use first int column or first column
    for col in df.columns:
        if pd.api.types.is_integer_dtype(df[col]):
            return str(df.at[row_index, col])
    return f"row_{row_index}"


def _already_marked(row_index: int, column_name: str) -> bool:
    """True if this row+column combo is already in the error log."""
    return any(
        e["row_index"] == row_index and e["column"] == column_name
        for e in _state["error_log"]
    )
