"""
schema_infer.py — builds a draft schema from the file scan and the user's rules.

The scan in schema_profile.py does the checking. Claude is only called when the user
wrote rules, to apply them and to drop any scan question those rules already answer.
Single cheap Claude call. Not an agent loop — just structured output.
"""
from __future__ import annotations

import json

import anthropic
import pandas as pd

from schema import ColumnSchema
from schema_profile import MAX_QUESTIONS, apply_patch, build_questions

PATCH_KEYS = {"dtype", "nullable", "min_value", "max_value", "allowed_values", "pattern", "date_format"}


def infer_schema(df: pd.DataFrame, rules: str = "") -> dict:
    """
    Parameters
    ----------
    df    : the uploaded CSV, read with dtype=str
    rules : optional plain-English rules from the user

    Returns
    -------
    {"schema": DataSchema dict, "questions": [...], "decisions": [...]}
    """
    draft = build_questions(df)
    if rules.strip():
        _apply_rules(draft, rules, df.head(3).to_dict(orient="records"))
    return draft


def _apply_rules(draft: dict, rules: str, sample_rows: list[dict]) -> None:
    client = anthropic.Anthropic()
    questions = [{k: q[k] for k in ("id", "column", "question")} for q in draft["questions"]]

    prompt = f"""You are a data schema assistant. A scan of the user's CSV produced this draft.

Draft schema:
{json.dumps(draft["schema"], indent=2)}

Questions the scan will ask the user:
{json.dumps(questions, indent=2)}

Sample rows:
{json.dumps(sample_rows, default=str, indent=2)}

User rules:
\"\"\"{rules}\"\"\"

Apply the user's rules. Return ONLY raw JSON — no markdown, no explanation, no code fences:
{{
  "column_changes": {{"<column>": {{"<field>": <value or null to clear>}}}},
  "answered_question_ids": ["<id of each question the rules already answer>"],
  "decisions": [{{"column": "<column>", "decision": "<what you set>", "reason": "<the rule that says so>"}}],
  "questions": [{{
    "column": "<column>",
    "question": "<one short question>",
    "why": "<what a wrong guess would change>",
    "options": [{{"label": "<short answer>", "patch": {{"<field>": <value or null>}}}}],
    "recommended": <index of the suggested option>
  }}]
}}

Fields: dtype (int, float, str, date), nullable, min_value, max_value, allowed_values, pattern,
date_format (YMD, DMY, MDY).

Only change what the rules say. Add a question only when the rules are unclear about something
that would change results, such as "price looks wrong" with no limit given. Never repeat or reword a
question listed above: list its id in answered_question_ids if the rules settle it, otherwise leave it.
Never guess a date order. Write decisions for someone who doesn't know the schema field names,
such as "Allow blanks" or "Maximum of 150", never "nullable: true" or "max_value: 150".
"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Strip accidental markdown fences
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    merge_rules(draft, json.loads(raw))


def merge_rules(draft: dict, result: dict) -> None:
    """Applies Claude's reply, dropping anything that wouldn't produce a valid schema."""
    columns = draft["schema"]["columns"]
    already_asked = {
        (q["column"], field) for q in draft["questions"] for option in q["options"] for field in option["patch"]
    }

    changed: set[tuple[str, str]] = set()
    for column, changes in result.get("column_changes", {}).items():
        if column in columns and _valid_patch(columns[column], changes):
            apply_patch(columns[column], changes)
            changed |= {(column, field) for field in changes}

    # Claude saying a question is answered isn't enough: the rules must have set what it asks about.
    answered = set(result.get("answered_question_ids", []))
    draft["questions"] = [
        q for q in draft["questions"]
        if not (q["id"] in answered and _question_fields(q) <= changed)
    ]

    draft["decisions"] = [d for d in result.get("decisions", []) if d.get("column") in columns] + draft["decisions"]

    for i, question in enumerate(result.get("questions", [])):
        if len(draft["questions"]) >= MAX_QUESTIONS:
            break
        column = question.get("column")
        options = question.get("options", [])
        if column not in columns or len(options) < 2:
            continue
        if not all(_valid_patch(columns[column], o.get("patch", {})) for o in options):
            continue
        if any((column, field) in already_asked for o in options for field in o["patch"]):
            continue
        recommended = question.get("recommended")
        if recommended not in range(len(options)):
            recommended = None
        else:
            apply_patch(columns[column], options[recommended]["patch"])
        draft["questions"].append({
            "id": f"rules:{i}:{column}",
            "column": column,
            "question": question.get("question", ""),
            "why": question.get("why", ""),
            "options": options,
            "recommended": recommended,
            "required": recommended is None,
        })


def _question_fields(question: dict) -> set[tuple[str, str]]:
    return {(question["column"], field) for option in question["options"] for field in option["patch"]}


def _valid_patch(column: dict, patch: dict) -> bool:
    if not isinstance(patch, dict) or not set(patch) <= PATCH_KEYS:
        return False
    merged = dict(column)
    apply_patch(merged, patch)
    try:
        ColumnSchema.model_validate(merged)
    except ValueError:
        return False
    return True
