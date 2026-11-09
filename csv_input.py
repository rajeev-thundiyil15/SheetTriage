"""
csv_input.py — turns an uploaded file into one standard CSV before anything else reads it.

Files come from Excel and other tools in different shapes: Windows encodings, semicolons
instead of commas, rows with stray extra values. Each upload is checked and rewritten once
as UTF-8 with commas, so the schema page, the agent and the review page all read the same
thing. Anything that can't be read without guessing is refused with a plain message.
"""
from __future__ import annotations

import csv
import io

DELIMITERS = [",", ";", "\t", "|"]
DELIMITER_NAMES = {",": "commas", ";": "semicolons", "\t": "tabs", "|": "pipes"}


def _decode(contents: bytes) -> str:
    if b"\x00" in contents:
        raise ValueError("This doesn't look like a CSV file. Save it as CSV (comma or semicolon separated) and try again.")
    try:
        return contents.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        # Excel on Windows saves CSVs in this encoding.
        return contents.decode("cp1252")
    except UnicodeDecodeError:
        raise ValueError("Couldn't read the text in this file. Save it as CSV UTF-8 and try again.")


def _delimiter(text: str) -> str:
    """The separator used most in the header row, outside quotes. Commas win a tie."""
    header = next(iter(text.splitlines()), "")
    counts = {d: next(csv.reader([header], delimiter=d), []).__len__() - 1 for d in DELIMITERS}
    best = max(DELIMITERS, key=lambda d: (counts[d], d == ","))
    return best if counts[best] > 0 else ","


def normalize_csv(contents: bytes) -> tuple[bytes, dict]:
    """
    Returns the file as UTF-8, comma-separated CSV bytes, plus notes on what was changed.
    Raises ValueError with a message for the user when the file can't be read safely.
    """
    text = _decode(contents)
    delimiter = _delimiter(text)
    rows = [row for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter) if row]

    if not rows:
        raise ValueError("The file is empty.")
    header = rows[0]
    width = len(header)
    if len(rows) == 1:
        raise ValueError("The file has a header row but no data rows.")

    out_rows = [header]
    for number, row in enumerate(rows[1:], start=2):
        if len(row) > width:
            extra = row[width:]
            if any(value.strip() for value in extra):
                raise ValueError(
                    f"Row {number} has {len(row)} values but the header has {width} columns, "
                    "so its values can't be matched to columns. Fix that row and upload again."
                )
            row = row[:width]  # trailing empty cells, as Excel sometimes writes
        out_rows.append(row)

    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(out_rows)
    notes = {"delimiter": DELIMITER_NAMES[delimiter]}
    return buffer.getvalue().encode("utf-8"), notes
