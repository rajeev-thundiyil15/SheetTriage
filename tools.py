"""
tools.py — pandas operations exposed to Claude as tools.

Design principle: never silently delete data.
- Fixable violations (typos, dtype coercion, date formats) → fix in place.
- Unfixable violations (can't infer correct value) → mark_row_error, keep the row.
- save_with_report writes two files: annotated CSV (_errors column) + report CSV.
"""
from __future__ import annotations

import re
import json
import pandas as pd
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
    df: pd.DataFrame = _state["df"]

    if column_name not in df.columns:
        return json.dumps({"error": f"Column '{column_name}' not in DataFrame"})
    if column_name not in schema.columns:
        return json.dumps({"error": f"Column '{column_name}' not in schema"})

    col_schema = schema.columns[column_name]
    violations: list[dict] = []

    for idx, val in df[column_name].items():
        # Skip rows already marked for this column
        if _already_marked(idx, column_name):
            continue

        issues = []

        if pd.isna(val):
            if not col_schema.nullable:
                issues.append("null value not allowed")
            if issues:
                violations.append({"row": int(idx), "value": None, "issues": issues})
            continue

        coerced = _try_coerce(val, col_schema.dtype)
        if coerced is None:
            issues.append(f"cannot convert '{val}' to {col_schema.dtype}")

        if coerced is not None and col_schema.dtype in ("int", "float"):
            if col_schema.min_value is not None and coerced < col_schema.min_value:
                issues.append(f"value {coerced} is below min {col_schema.min_value}")
            if col_schema.max_value is not None and coerced > col_schema.max_value:
                issues.append(f"value {coerced} is above max {col_schema.max_value}")

        if col_schema.allowed_values and str(val) not in col_schema.allowed_values:
            issues.append(f"'{val}' not in allowed values {col_schema.allowed_values}")

        if col_schema.pattern and not re.fullmatch(col_schema.pattern, str(val)):
            issues.append(f"'{val}' does not match pattern {col_schema.pattern}")

        if issues:
            violations.append({"row": int(idx), "value": str(val), "issues": issues})

    return json.dumps({
        "column": column_name,
        "total_violations": len(violations),
        "violations": violations,
    }, indent=2)


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
    entry = f"{column_name}: {issue}"
    df.at[row_index, "_errors"] = (current + "; " + entry).lstrip("; ")

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
    if log:
        report_df = pd.DataFrame(log, columns=["row_id", "column", "original_value", "error", "row_index"])
        report_df = report_df.drop(columns=["row_index"])
        report_df.insert(0, "action_needed", "Review & correct manually")
    else:
        report_df = pd.DataFrame(columns=["row_id", "column", "original_value", "error", "action_needed"])

    Path(report_path).parent.mkdir(parents=True, exist_ok=True)
    report_df.to_csv(report_path, index=False)

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
