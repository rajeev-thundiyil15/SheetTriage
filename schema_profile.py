"""
schema_profile.py — scans an uploaded CSV and works out what to ask the user.

No API calls. A wrong guess that changes data silently becomes a question; anything
the data settles becomes a decision, with its reason shown. Option patches set schema
fields directly, and a None value clears a field, so answers never need interpreting.
"""
from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher

import pandas as pd

from tools import NUMERIC_DATE, YEAR_FIRST_DATE

MAX_QUESTIONS = 5
MAX_LIST_VALUES = 20
FUZZY_MATCH = 0.8
TEXT_DATE = re.compile(r"^([A-Za-z]{3,9}\.? \d{1,2},? \d{4}|\d{1,2} [A-Za-z]{3,9}\.? \d{4})$")


def _is_number(value: str) -> bool:
    try:
        return float(value) == float(value)
    except ValueError:
        return False


def _date_counts(values: list[str]) -> dict:
    counts = {"year_first": 0, "numeric": 0, "text": 0, "first_over_12": 0, "second_over_12": 0, "bad": []}
    for value in values:
        match = NUMERIC_DATE.match(value)
        if YEAR_FIRST_DATE.match(value):
            counts["year_first"] += 1
        elif match:
            counts["numeric"] += 1
            counts["first_over_12"] += int(match.group(1)) > 12
            counts["second_over_12"] += int(match.group(2)) > 12
        elif TEXT_DATE.match(value):
            counts["text"] += 1
        else:
            counts["bad"].append(value)
    return counts


def _looks_like_date(column: str, values: list[str], counts: dict) -> bool:
    if not values:
        return False
    matched = (len(values) - len(counts["bad"])) / len(values)
    named = bool(re.search(r"date|_at$|_on$|restock", column, re.IGNORECASE))
    return matched >= 0.8 or (named and matched >= 0.5)


def _readings(values: list[str]) -> str:
    """A worked example of how one ambiguous date reads each way."""
    for value in values:
        match = NUMERIC_DATE.match(value)
        if match and match.group(1) != match.group(2):
            first, second = int(match.group(1)), int(match.group(2))
            if first <= 12 and second <= 12:
                return value
    return values[0]


def _value_clusters(counts: Counter) -> dict[str, list[str]]:
    """Maps each canonical value to its likely misspellings and case variants."""
    clusters: dict[str, list[str]] = {}
    for value, count in counts.most_common():
        for canonical in clusters:
            same_text = value.strip().casefold() == canonical.strip().casefold()
            similar = SequenceMatcher(None, value.casefold(), canonical.casefold()).ratio() >= FUZZY_MATCH
            if same_text or (similar and count <= counts[canonical] / 2):
                clusters[canonical].append(value)
                break
        else:
            clusters[value] = []
    return clusters


def _date_findings(column: str, values: list[str], counts: dict) -> tuple[dict, list[dict], list[dict]]:
    patch: dict = {"dtype": "date", "date_format": None}
    decisions: list[dict] = []
    questions: list[dict] = []

    if counts["numeric"] == 0:
        patch["date_format"] = "YMD" if counts["year_first"] else None
        decisions.append({"column": column, "decision": "Treat as a date", "reason": "The dates say which part is the month."})
    elif counts["first_over_12"] and not counts["second_over_12"]:
        patch["date_format"] = "DMY"
        decisions.append({
            "column": column,
            "decision": "Read dates as day first",
            "reason": f"{counts['first_over_12']:,} date(s) start with a number above 12, which can only be a day.",
        })
    elif counts["second_over_12"] and not counts["first_over_12"]:
        patch["date_format"] = "MDY"
        decisions.append({
            "column": column,
            "decision": "Read dates as month first",
            "reason": f"{counts['second_over_12']:,} date(s) have a number above 12 in the middle, which can only be a day.",
        })
    else:
        example = _readings([v for v in values if NUMERIC_DATE.match(v)])
        mixed = counts["first_over_12"] and counts["second_over_12"]
        questions.append({
            "id": f"date_format:{column}",
            "column": column,
            "priority": 0,
            "required": True,
            "question": f"How should dates like {example} in {column} be read?",
            "why": (
                "This column seems to mix both date orders, so one choice will flag the others."
                if mixed else
                f"{counts['numeric']:,} date(s) could be read either way, and a wrong guess would change them without any warning."
            ),
            "options": [
                {"label": "Day first (DD/MM/YYYY)", "patch": {"date_format": "DMY"}},
                {"label": "Month first (MM/DD/YYYY)", "patch": {"date_format": "MDY"}},
                {"label": "Not sure: flag them for review", "patch": {"date_format": None}},
            ],
            "recommended": None,
        })

    if counts["bad"]:
        decisions.append({
            "column": column,
            "decision": f"Flag {len(counts['bad']):,} value(s) that aren't dates",
            "reason": f"For example '{counts['bad'][0]}'. The original text stays in your file.",
        })
    return patch, decisions, questions


def _number_findings(column: str, values: list[str], numeric: list[float]) -> tuple[dict, list[dict], list[dict]]:
    dtype = "int" if all(n == int(n) for n in numeric) else "float"
    patch: dict = {"dtype": dtype}
    decisions: list[dict] = []
    questions: list[dict] = []

    bad = [v for v in values if not _is_number(v)]
    if bad:
        questions.append({
            "id": f"not_number:{column}",
            "column": column,
            "priority": 2,
            "question": f"{column} is mostly numbers, but {len(bad):,} value(s) aren't, such as '{bad[0]}'. Is it a number column?",
            "why": f"{len(numeric):,} of {len(values):,} filled values are numbers.",
            "options": [
                {"label": "Yes: flag the values that aren't numbers", "patch": {"dtype": dtype}},
                {"label": "No: treat it as text", "patch": {"dtype": "str", "min_value": None, "max_value": None}},
            ],
            "recommended": 0,
        })
    else:
        decisions.append({
            "column": column,
            "decision": "Treat as a whole number" if dtype == "int" else "Treat as a decimal number",
            "reason": f"All {len(values):,} filled values fit.",
        })

    negatives = sum(n < 0 for n in numeric)
    if 0 < negatives <= len(numeric) * 0.2:
        questions.append({
            "id": f"negative:{column}",
            "column": column,
            "priority": 3,
            "question": f"Can {column} be negative? {negatives:,} value(s) are below zero.",
            "why": "A small share of negatives is often a data entry mistake, but refunds or adjustments can be real.",
            "options": [
                {"label": "No: flag negative values", "patch": {"min_value": 0}},
                {"label": "Yes: negatives are valid", "patch": {"min_value": None}},
            ],
            "recommended": 0,
        })
    return patch, decisions, questions


def _text_findings(column: str, values: list[str]) -> tuple[dict, list[dict], list[dict]]:
    patch: dict = {"dtype": "str"}
    decisions: list[dict] = []
    questions: list[dict] = []
    counts = Counter(values)

    if re.search(r"e-?mail", column, re.IGNORECASE):
        patch["pattern"] = r".+@.+\..+"
        decisions.append({"column": column, "decision": "Check for valid email addresses", "reason": "The column name says it holds emails."})
    elif 2 <= len(counts) <= MAX_LIST_VALUES and len(values) >= 20 and len(counts) <= len(values) / 2:
        clusters = _value_clusters(counts)
        canonical = list(clusters)
        variants = [f"'{v}' looks like '{c}'" for c, vs in clusters.items() for v in vs]
        questions.append({
            "id": f"allowed:{column}",
            "column": column,
            "priority": 1,
            "question": f"Should {column} only allow these values: {', '.join(canonical)}?",
            "why": ". ".join(variants[:3]) + ". Clean mode can correct these." if variants else f"Only {len(canonical)} different values appear across {len(values):,} rows.",
            "options": [
                {"label": f"Yes, only these {len(canonical)}", "patch": {"allowed_values": canonical}},
                {"label": "No, allow any text", "patch": {"allowed_values": None}},
            ],
            "recommended": 0,
        })
    return patch, decisions, questions


def build_questions(df: pd.DataFrame) -> dict:
    """
    df must be read with dtype=str so values stay as the user typed them.
    Returns a schema with the suggested answers applied, plus questions and decisions.
    """
    columns: dict[str, dict] = {}
    questions: list[dict] = []
    decisions: list[dict] = []

    for column in df.columns:
        series = df[column]
        values = [str(v).strip() for v in series.dropna() if str(v).strip()]
        blanks = len(series) - len(values)
        date_counts = _date_counts(values)
        numeric = [float(v) for v in values if _is_number(v)]

        if _looks_like_date(column, values, date_counts):
            patch, found_decisions, found_questions = _date_findings(column, values, date_counts)
        elif values and len(numeric) / len(values) >= 0.7:
            patch, found_decisions, found_questions = _number_findings(column, values, numeric)
        else:
            patch, found_decisions, found_questions = _text_findings(column, values)

        columns[column] = {"nullable": False}
        apply_patch(columns[column], patch)
        decisions += found_decisions
        questions += found_questions

        if blanks:
            share = blanks / len(series)
            questions.append({
                "id": f"blank:{column}",
                "column": column,
                "priority": 4,
                "question": f"Can {column} be left blank? {blanks:,} row(s), {share:.1%}, are empty.",
                "why": "A few blanks usually means missing data. Many blanks usually means the field is optional.",
                "options": [
                    {"label": "No: flag blank rows", "patch": {"nullable": False}},
                    {"label": "Yes: blanks are fine", "patch": {"nullable": True}},
                ],
                "recommended": 0 if share < 0.05 else 1,
            })

    questions.sort(key=lambda q: q["priority"])
    shown = [q for i, q in enumerate(questions) if q.get("required") or i < MAX_QUESTIONS]
    shown_ids = {q["id"] for q in shown}

    for question in questions:
        if question["recommended"] is not None:
            apply_patch(columns[question["column"]], question["options"][question["recommended"]]["patch"])
        if question["id"] not in shown_ids:
            decisions.append({
                "column": question["column"],
                "decision": question["options"][question["recommended"]]["label"],
                "reason": question["question"],
            })

    return {"schema": {"name": "dataset", "columns": columns}, "questions": shown, "decisions": decisions}


def apply_patch(column: dict, patch: dict) -> None:
    for key, value in patch.items():
        if value is None:
            column.pop(key, None)
        else:
            column[key] = value
