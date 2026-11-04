"""
review.py — lines the output up with the original file so people can review a run on the page.

Row numbers match the original file as seen in a spreadsheet: the header is row 1.
"""
from __future__ import annotations

import io

import pandas as pd

HEADER_ROWS = 1


def _same_value(before: str, after: str) -> bool:
    """True when a cell only changed format, such as 65500 becoming 65500.0."""
    if before.strip() == after.strip():
        return True
    try:
        return float(before) == float(after)
    except ValueError:
        return False


def build_review(original_csv: bytes, output_path: str, error_log: list[dict]) -> dict:
    original = pd.read_csv(io.BytesIO(original_csv), dtype=str, keep_default_na=False)
    output = pd.read_csv(output_path, dtype=str, keep_default_na=False)

    flagged = {e["row_index"] for e in error_log}
    removed_flagged = "_errors" not in output.columns

    # Strict mode drops flagged rows, so the rows left are the unflagged ones, in order.
    source_rows = [i for i in range(len(original)) if i not in flagged] if removed_flagged else list(range(len(output)))
    aligned = len(source_rows) == len(output)

    issues_by_cell: dict[tuple[int, str], list[str]] = {}
    for entry in error_log:
        issues_by_cell.setdefault((entry["row_index"], entry["column"]), []).append(entry["error"])

    columns = [c for c in output.columns if c != "_errors"]
    rows = []
    changed_cells = 0
    changed_rows = 0

    for position, values in enumerate(output[columns].itertuples(index=False, name=None)):
        source = source_rows[position] if aligned else None
        issues: dict[str, list[str]] = {}
        changes: dict[str, str] = {}
        if source is not None:
            for column, value in zip(columns, values):
                if (source, column) in issues_by_cell:
                    issues[column] = issues_by_cell[(source, column)]
                elif column in original.columns:
                    before = original.iat[source, original.columns.get_loc(column)]
                    if not _same_value(before, value):
                        changes[column] = before
        changed_cells += len(changes)
        changed_rows += bool(changes)
        rows.append({
            "row": None if source is None else source + HEADER_ROWS + 1,
            "values": list(values),
            "issues": issues,
            "changes": changes,
        })

    report = [
        {
            "row": e["row_index"] + HEADER_ROWS + 1,
            "row_id": e["row_id"],
            "column": e["column"],
            "value": e["original_value"],
            "issue": e["error"],
        }
        for e in sorted(error_log, key=lambda e: e["row_index"])
    ]

    return {
        "columns": columns,
        "rows": rows,
        "report": report,
        "removed_flagged": removed_flagged,
        "rows_in_original": len(original),
        "rows_in_output": len(output),
        "flagged_rows": len(flagged),
        "changed_cells": changed_cells,
        "changed_rows": changed_rows,
    }
