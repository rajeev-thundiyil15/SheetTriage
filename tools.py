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
from pathlib import Path
from typing import Any

from schema import DataSchema


_state: dict[str, Any] = {
    "df": None,
    "error_log": [],   # [{row_index, id_value, column, original_value, error}]
}


def set_dataframe(df: pd.DataFrame) -> None:
    _state["df"] = df.copy()
    _state["error_log"] = []


def get_dataframe() -> pd.DataFrame:
    return _state["df"]


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

def validate_column(column_name: str, schema: DataSchema) -> str:
    """
    Returns violations grouped by the exact issue text, each with the full list of
    row indices that have it — e.g. one group for "cannot convert 'sixty five' to
    float" covering 40 rows, instead of 40 near-identical entries. Keeps the payload
    small on files with many repeated errors (the common case) without ever dropping
    a row: every violating row index appears in exactly one group.
    """
    df: pd.DataFrame = _state["df"]

    if column_name not in df.columns:
        return json.dumps({"error": f"Column '{column_name}' not in DataFrame"})
    if column_name not in schema.columns:
        return json.dumps({"error": f"Column '{column_name}' not in schema"})

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

        coerced = _try_coerce(val, col_schema.dtype)
        if coerced is None:
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

    if not flat:
        return json.dumps({"column": column_name, "total_violations": 0, "groups": []})

    groups: dict[str, dict] = {}
    for row_idx, issue in flat:
        g = groups.setdefault(issue, {"issue": issue, "count": 0, "rows": []})
        g["count"] += 1
        g["rows"].append(row_idx)

    group_list = sorted(groups.values(), key=lambda g: -g["count"])

    MAX_GROUPS_SHOWN = 40
    result = {
        "column": column_name,
        "total_violations": len(flat),
        "distinct_issue_groups": len(group_list),
        "groups": group_list[:MAX_GROUPS_SHOWN],
    }
    if len(group_list) > MAX_GROUPS_SHOWN:
        result["note"] = (
            f"Showing the {MAX_GROUPS_SHOWN} most common of {len(group_list)} distinct issue "
            "groups. After fixing or marking these, call validate_column again on this column — "
            "already-handled rows are skipped automatically, so the next-most-common remaining "
            "groups will surface."
        )

    return json.dumps(result, indent=2)


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
    original_value = str(df.at[row_index, column_name]) if column_name in df.columns else "(unknown)"

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
            mark_row_error(
                int(v["row_index"]),
                v["column_name"],
                v["issue"],
                v.get("id_column", ""),
            )
            marked += 1
        except Exception as exc:
            errors.append(str(exc))
    result = {"marked": marked}
    if errors:
        result["errors"] = errors[:5]
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Fixes (for violations that CAN be auto-corrected)
# ---------------------------------------------------------------------------

def fix_dtype(column_name: str, target_dtype: str) -> str:
    """Coerce a column to the target dtype. Values that fail become NaN."""
    df: pd.DataFrame = _state["df"]
    original = df[column_name].copy()

    if target_dtype == "int":
        df[column_name] = pd.to_numeric(df[column_name], errors="coerce").astype("Int64")
    elif target_dtype == "float":
        df[column_name] = pd.to_numeric(df[column_name], errors="coerce")
    elif target_dtype == "str":
        df[column_name] = df[column_name].astype(str)
    elif target_dtype == "date":
        df[column_name] = pd.to_datetime(df[column_name], errors="coerce", dayfirst=True)

    converted, failed = 0, 0
    for old, new in zip(original, df[column_name]):
        if pd.isna(new) and not pd.isna(old):
            failed += 1
        elif not pd.isna(new):
            converted += 1

    return json.dumps({
        "column": column_name,
        "dtype_applied": target_dtype,
        "successfully_converted": converted,
        "failed_to_convert": failed,
        "note": (
            f"{failed} value(s) could not be converted and are now NaN. "
            "Call validate_column again — then mark_row_error for any remaining NaN violations."
        ) if failed else "",
    })


def fix_allowed_values(column_name: str, replacements: dict[str, str]) -> str:
    """Replace disallowed values with correct ones (typos, casing, etc.)."""
    df: pd.DataFrame = _state["df"]
    total_replaced = 0
    for bad, good in replacements.items():
        mask = df[column_name] == bad
        count = int(mask.sum())
        if count:
            df.loc[mask, column_name] = good
            total_replaced += count

    return json.dumps({
        "column": column_name,
        "replacements_applied": replacements,
        "total_cells_changed": total_replaced,
    })


def fill_null(column_name: str, fill_value: str) -> str:
    """Fill null values with a known correct value (only use when the value is certain)."""
    df: pd.DataFrame = _state["df"]
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
) -> str:
    """
    Writes two files:
    1. CSV output — all rows (or only clean rows when exclude_error_rows=True), _errors column added.
    2. Report CSV — one row per violation: who, which field, what's wrong.

    exclude_error_rows=True → strict mode: only rows with no errors are saved to the CSV.
    """
    df: pd.DataFrame = _state["df"]

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

    with open(report_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)

        if log:
            # ── Summary block ──
            col_counts = Counter(e["column"] for e in log)
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
    })


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _try_coerce(value: Any, dtype: str) -> Any:
    try:
        if dtype == "int":   return int(float(str(value)))
        if dtype == "float": return float(str(value))
        if dtype == "str":   return str(value)
        if dtype == "date":  return pd.to_datetime(value, dayfirst=True)
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
