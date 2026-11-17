"""
suggestions.py — proposed fixes for flagged cells, and likely duplicate rows, for a person to approve.

Nothing here changes a file on its own. For each flagged cell the code works out the
fixes that are possible (the closest allowed values, the other reading of a date, the
number without its currency sign), and Claude recommends one of them with a reason, or
says it isn't sure. Claude can only pick from the options the code found, so it can't
invent a value. A person then accepts a fix, says the value is correct, or leaves it.

Duplicates are found by code alone: rows that match in every column except an ID column.
"""
from __future__ import annotations

import calendar
import csv
import io
import json
import re
from collections import Counter
from difflib import SequenceMatcher

import pandas as pd

from schema import DataSchema
from tools import NUMERIC_DATE, _parse_date

HEADER_ROWS = 1
MAX_RANKED = 200          # distinct values sent to Claude in one run; cells sharing a value share the answer
MAX_OPTIONS = 3
SIMILAR_ENOUGH = 0.5      # looser than the automatic typo rule: these are options for a person to pick
NEAR_THE_BEST = 0.25      # and only those nearly as close as the best one

NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty".split())}
CURRENCY = re.compile(r"^\s*(?:[$€£¥₹]|USD|EUR|GBP|INR|CAD|AUD)\s*([-+]?[\d,]*\.?\d+)\s*(?:[$€£¥₹]|USD|EUR|GBP)?\s*$", re.I)
THOUSANDS = re.compile(r"^\s*[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*$")
ID_NAME = re.compile(r"(^|[_\s])(id|no|number|num|ref|code|key)$", re.I)

CELL_ACTIONS = ("accept", "keep", "leave")
DUPLICATE_ACTIONS = ("remove", "different", "leave")


# ── Options for one flagged cell ──

def _number_text(value: float, dtype: str) -> str:
    return str(int(value)) if dtype == "int" else f"{value:.2f}"


def _in_range(value: float, rules) -> bool:
    return ((rules.min_value is None or value >= rules.min_value)
            and (rules.max_value is None or value <= rules.max_value))


def _allowed_options(value: str, rules, counts: Counter) -> list[dict]:
    allowed = rules.allowed_values or []
    scored = sorted(
        ((SequenceMatcher(None, value.casefold(), str(a).casefold()).ratio(), str(a)) for a in allowed),
        reverse=True)
    best = scored[0][0] if scored else 0
    return [{"value": a, "note": f"a close allowed value, used {counts.get(a, 0)} times in this column"}
            for score, a in scored[:MAX_OPTIONS] if score >= SIMILAR_ENOUGH and score >= best - NEAR_THE_BEST]


def _date_options(value: str) -> list[dict]:
    match = NUMERIC_DATE.match(value.strip())
    if not match:
        return []
    a, b, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    year = year + 2000 if year < 100 else year
    options = []
    for month, day, note in ((a, b, "read month first"), (b, a, "read day first")):
        try:
            date = pd.Timestamp(year=year, month=month, day=day)
        except ValueError:
            continue
        text = date.strftime("%Y-%m-%d")
        if text not in [o["value"] for o in options]:
            options.append({"value": text, "note": note})
    if options:
        return options
    # A day that doesn't exist in its month, such as 02/29 in a non-leap year.
    for month, day in ((a, b), (b, a)):
        if 1 <= month <= 12 and day > calendar.monthrange(year, month)[1]:
            last = pd.Timestamp(year=year, month=month, day=calendar.monthrange(year, month)[1])
            following = last + pd.Timedelta(days=1)
            return [{"value": last.strftime("%Y-%m-%d"), "note": "last day of that month"},
                    {"value": following.strftime("%Y-%m-%d"), "note": "first day of the next month"}]
    return []


def _number_options(value: str, rules) -> list[dict]:
    dtype = rules.dtype
    text = value.strip()
    candidates = []
    if (m := CURRENCY.match(text)):
        candidates.append((float(m.group(1).replace(",", "")), "without the currency sign; check the currency"))
    elif THOUSANDS.match(text):
        candidates.append((float(text.replace(",", "")), "without the thousands separator"))
    elif text.casefold() in NUMBER_WORDS:
        candidates.append((float(NUMBER_WORDS[text.casefold()]), "the word written as a number"))
    else:
        try:
            number = float(text)
        except ValueError:
            return []
        if dtype == "int" and number != int(number):
            low, high = int(number // 1), int(number // 1) + 1
            candidates += [(float(low), "rounded down"), (float(high), "rounded up")]
    options = []
    for number, note in candidates:
        if dtype == "int" and number != int(number):
            continue
        if _in_range(number, rules):
            options.append({"value": _number_text(number, dtype), "note": note})
    return options


def _range_options(value: str, rules) -> list[dict]:
    try:
        number = float(value)
    except ValueError:
        return []
    if number < 0 and _in_range(-number, rules):
        return [{"value": _number_text(-number, rules.dtype), "note": "without the minus sign"}]
    return []


def _pattern_options(value: str, rules) -> list[dict]:
    squeezed = re.sub(r"\s+", "", value)
    if squeezed != value and rules.pattern and re.fullmatch(rules.pattern, squeezed):
        return [{"value": squeezed, "note": "without the spaces"}]
    return []


def cell_options(value: str, issue: str, rules, counts: Counter) -> list[dict]:
    """The fixes that are possible for one flagged cell, most likely first by the code's reckoning."""
    if value in ("", "(blank)") or "null value not allowed" in issue:
        return []
    if "not in allowed values" in issue:
        return _allowed_options(value, rules, counts)
    if "cannot convert" in issue and issue.rstrip().endswith("to date"):
        return _date_options(value)
    if "cannot convert" in issue:
        return _number_options(value, rules)
    if "is below min" in issue or "is above max" in issue:
        return _range_options(value, rules)
    if "does not match pattern" in issue:
        return _pattern_options(value, rules)
    return []


# ── Duplicates ──

def _id_columns(df: pd.DataFrame) -> list[str]:
    """Columns that name each row, such as order_id: an ID-like name and a different value in every row."""
    return [c for c in df.columns if ID_NAME.search(str(c)) and df[c].nunique(dropna=False) == len(df)]


def find_duplicates(df: pd.DataFrame) -> list[dict]:
    """Groups of rows that match in every column except ID columns, ignoring case and spaces."""
    data = df.drop(columns=["_errors"], errors="ignore")
    ids = _id_columns(data)
    compare = [c for c in data.columns if c not in ids]
    if len(data) < 2 or not compare:
        return []
    keys = data[compare].astype(str).apply(lambda col: col.str.strip().str.casefold())
    groups = []
    for _, rows in keys.groupby(compare, dropna=False, sort=False).groups.items():
        positions = sorted(int(data.index.get_loc(i)) for i in rows)
        if len(positions) > 1:
            groups.append({"positions": positions, "ignored": ids})
    return sorted(groups, key=lambda g: g["positions"][0])


# ── Putting them together ──

def build_suggestions(output: pd.DataFrame, report: list[dict], schema: DataSchema, rank=None) -> list[dict]:
    """Every flagged cell (with its options) and every likely duplicate, ready for the review page.

    rank(cells) returns {id: (option index or None, reason)}; see rank_with_ai."""
    suggestions = []
    cells = {}
    counts_by_column = {}
    for entry in report:
        key = (entry["row"], entry["column"])
        if key in cells:
            cells[key]["issues"].append(entry["issue"])
            continue
        rules = schema.columns.get(entry["column"])
        if entry["column"] not in counts_by_column:
            counts_by_column[entry["column"]] = (
                Counter(output[entry["column"]].astype(str)) if entry["column"] in output.columns else Counter())
        counts = counts_by_column[entry["column"]]
        options = cell_options(str(entry["value"]), entry["issue"], rules, counts) if rules else []
        cells[key] = {
            "id": f"c{len(cells) + 1}",
            "kind": "cell",
            "row": entry["row"],
            "row_id": entry["row_id"],
            "column": entry["column"],
            "value": entry["value"],
            "issues": [entry["issue"]],
            "options": options,
            "recommended": None,
            "reason": "",
        }
    suggestions.extend(cells.values())

    # Cells with the same value and the same options get the same answer, so Claude is asked once
    # per distinct value: a file with Credit Card in 40 rows needs one recommendation, not 40.
    groups = {}
    for s in suggestions:
        if s["options"]:
            key = (s["column"], s["value"], s["issues"][0], tuple(o["value"] for o in s["options"]))
            groups.setdefault(key, []).append(s)
    asked = [cells_alike[0] for cells_alike in list(groups.values())[:MAX_RANKED]]
    if rank and asked:
        picks = rank(asked)
        for first in asked:
            pick, reason = picks.get(first["id"], (None, ""))
            key = (first["column"], first["value"], first["issues"][0], tuple(o["value"] for o in first["options"]))
            for s in groups[key]:
                if isinstance(pick, int) and 0 <= pick < len(s["options"]):
                    s["recommended"] = pick
                s["reason"] = str(reason or "")[:300]

    for n, group in enumerate(find_duplicates(output), start=1):
        rows = [p + HEADER_ROWS + 1 for p in group["positions"]]
        ignored = ", ".join(group["ignored"])
        suggestions.append({
            "id": f"d{n}",
            "kind": "duplicate",
            "rows": rows,
            "reason": (f"Every column matches except {ignored}." if ignored else "Every column matches.")
                      + " Removing keeps the first one.",
        })
    return suggestions


# ── Claude's recommendation ──

RANK_TOOL = {
    "name": "recommend_fixes",
    "description": "Record which option, if any, is the right fix for each flagged cell.",
    "input_schema": {
        "type": "object",
        "properties": {
            "fixes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "pick": {"type": ["integer", "null"], "description": "Index of the option, or null when no option is clearly right"},
                        "reason": {"type": "string", "description": "One plain sentence a non-technical person can check"},
                    },
                    "required": ["id", "pick", "reason"],
                },
            },
        },
        "required": ["fixes"],
    },
}

RANK_PROMPT = """You help someone review a spreadsheet. Each item below is a cell that broke a rule, with the fixes that are possible. For each one, pick the option that is clearly the intended value, or null when you can't tell from the evidence. Prefer null to a guess: the person decides either way, and a wrong recommendation costs them more than none.

Evidence you can use: the value itself, the issue, each option's note, and how often each option already appears in the column. A close misspelling of a common value is usually that value. A date is usually the reading that matches the column's date order. A currency sign means the number may be in another currency, so say so.

Write each reason as one short plain sentence, without jargon. Items:

"""


def rank_with_ai(cells: list[dict], on_log=None) -> dict:
    """Asks Claude which option fits each cell. Returns {id: (index or None, reason)}; {} if Claude can't be reached."""
    import anthropic

    from agent import MODEL, usage_event

    items = [{
        "id": c["id"], "column": c["column"], "value": c["value"], "issue": c["issues"][0],
        "options": [{"index": i, "value": o["value"], "note": o["note"]} for i, o in enumerate(c["options"])],
    } for c in cells]
    try:
        response = anthropic.Anthropic().messages.create(
            model=MODEL,
            max_tokens=4096,
            tools=[RANK_TOOL],
            tool_choice={"type": "tool", "name": RANK_TOOL["name"]},
            messages=[{"role": "user", "content": RANK_PROMPT + json.dumps(items, ensure_ascii=False)}],
        )
    except Exception:
        return {}
    if on_log:
        on_log(usage_event(response))
    for block in response.content:
        if getattr(block, "type", "") == "tool_use":
            fixes = block.input.get("fixes", []) if isinstance(block.input, dict) else []
            return {f.get("id"): (f.get("pick"), f.get("reason", "")) for f in fixes if isinstance(f, dict)}
    return {}


# ── Applying a person's decisions ──

def resolved_cells(suggestions: list[dict], decisions: dict) -> dict:
    """(row, column) -> ("accept", new value) or ("keep", None), for cells someone has decided."""
    resolved = {}
    for s in suggestions:
        d = decisions.get(s["id"])
        if s["kind"] != "cell" or not d:
            continue
        if d["action"] == "accept":
            resolved[(s["row"], s["column"])] = ("accept", s["options"][d["option"]]["value"])
        elif d["action"] == "keep":
            resolved[(s["row"], s["column"])] = ("keep", None)
    return resolved


def removed_rows(suggestions: list[dict], decisions: dict) -> dict:
    """Row number -> the row it duplicates, for copies someone chose to remove."""
    removed = {}
    for s in suggestions:
        d = decisions.get(s["id"])
        if s["kind"] == "duplicate" and d and d["action"] == "remove":
            for row in s["rows"][1:]:
                removed[row] = s["rows"][0]
    return removed


def _by_row(resolved: dict) -> dict:
    """The decided cells grouped by row number, so large files aren't searched once per decision."""
    by_row = {}
    for (row, column), (action, value) in resolved.items():
        by_row.setdefault(row, []).append((column, action, value))
    return by_row


def apply_to_review(review: dict, suggestions: list[dict], decisions: dict) -> dict:
    """The review as the page shows it, with every decision applied, plus the suggestions and their status."""
    review = json.loads(json.dumps(review))  # a copy: the saved review stays as the run left it
    resolved = resolved_cells(suggestions, decisions)
    removed = removed_rows(suggestions, decisions)
    by_row = _by_row(resolved)
    columns = review["columns"]
    for row in review["rows"]:
        row["approved"], row["kept"] = {}, []
        for column, action, value in by_row.get(row["row"], []):
            if column not in columns:
                continue
            row["issues"].pop(column, None)
            if action == "accept":
                i = columns.index(column)
                row["approved"][column] = row["values"][i]
                row["values"][i] = value
            else:
                row["kept"].append(column)
        if row["row"] in removed:
            row["removed_as_copy_of"] = removed[row["row"]]
    review["report"] = [e for e in review["report"] if (e["row"], e["column"]) not in resolved]
    review["flagged_rows"] = sum(1 for r in review["rows"] if r["issues"] and not r.get("removed_as_copy_of"))
    review["approved_cells"] = sum(1 for a, _ in resolved.values() if a == "accept")
    review["kept_cells"] = sum(1 for a, _ in resolved.values() if a == "keep")
    review["removed_rows"] = len(removed)
    review["suggestions"] = [dict(s, decision=decisions.get(s["id"])) for s in suggestions]
    return review


def decided_output_csv(output_csv: str, review: dict, suggestions: list[dict], decisions: dict) -> str:
    """The cleaned CSV with accepted fixes, resolved flags taken out of _errors, and removed copies left out."""
    df = pd.read_csv(io.StringIO(output_csv), dtype=str, keep_default_na=False)
    resolved = resolved_cells(suggestions, decisions)
    removed = removed_rows(suggestions, decisions)
    resolved_keys = set(resolved)
    remaining = {}
    for e in review["report"]:
        if (e["row"], e["column"]) not in resolved_keys:
            remaining.setdefault(e["row"], []).append(e["issue"])
    for number, cells in _by_row(resolved).items():
        position = number - HEADER_ROWS - 1
        if not 0 <= position < len(df):
            continue
        for column, action, value in cells:
            if action == "accept" and column in df.columns:
                df.iat[position, df.columns.get_loc(column)] = value
        if "_errors" in df.columns:
            df.iat[position, df.columns.get_loc("_errors")] = "; ".join(remaining.get(number, []))
    keep = [position + HEADER_ROWS + 1 not in removed for position in range(len(df))]
    out = io.StringIO()
    df[keep].to_csv(out, index=False)
    return out.getvalue()


def decided_report_csv(review: dict, suggestions: list[dict], decisions: dict) -> str:
    """The error report in the usual layout, listing only the problems still open."""
    entries = apply_to_review(review, suggestions, decisions)["report"]
    out = io.StringIO()
    w = csv.writer(out)
    if not entries:
        w.writerow(["No errors left."])
        return out.getvalue()
    counts = Counter(e["column"] for e in entries)
    w.writerow(["SUMMARY"])
    w.writerow(["column", "error_count"])
    for column, count in sorted(counts.items(), key=lambda x: -x[1]):
        w.writerow([column, count])
    w.writerow([])
    w.writerow(["total_errors", len(entries)])
    w.writerow(["rows_affected", len({e["row"] for e in entries})])
    w.writerow([])
    w.writerow(["DETAILS"])
    w.writerow(["row_id", "column", "original_value", "error", "action_needed"])
    for e in entries:
        w.writerow([e["row_id"], e["column"], e["value"], e["issue"], "Review & correct manually"])
    return out.getvalue()


def check_decision(suggestion: dict, decision: dict) -> dict | None:
    """The decision cleaned up, or None if it isn't one this suggestion allows."""
    action = decision.get("action")
    if suggestion["kind"] == "cell":
        if action == "accept":
            option = decision.get("option")
            if isinstance(option, int) and 0 <= option < len(suggestion["options"]):
                return {"action": "accept", "option": option}
            return None
        return {"action": action} if action in ("keep", "leave") else None
    return {"action": action} if action in DUPLICATE_ACTIONS else None
