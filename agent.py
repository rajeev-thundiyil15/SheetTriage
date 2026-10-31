"""
agent.py — The agentic loop.

Tool philosophy (matches tools.py):
- Fix what can be safely auto-corrected: typos, dtype coercion, date formats.
- For violations that cannot be safely inferred (null required field, out-of-range
  with no correct substitute, invalid email) → call mark_row_error. Never drop rows.
- At the end, save_with_report writes the annotated CSV + a separate report CSV.
"""
from __future__ import annotations

import json
from typing import Callable
import anthropic
import pandas as pd

from schema import DataSchema
import tools as t


def derive_report_path(output_path: str) -> str:
    """data/abc_clean.csv -> data/abc_report.csv; data/abc.csv -> data/abc_report.csv"""
    if output_path.endswith("_clean.csv"):
        return output_path[: -len("_clean.csv")] + "_report.csv"
    if output_path.endswith(".csv"):
        return output_path[: -len(".csv")] + "_report.csv"
    return output_path + "_report.csv"


TOOL_DEFINITIONS = [
    {
        "name": "inspect_data",
        "description": (
            "Get a snapshot of the current DataFrame: shape, column names, dtypes, "
            "null counts, and the first 5 rows. Always call this first."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "validate_column",
        "description": (
            "Validate one column against the schema. Returns violations GROUPED by "
            "the exact issue text — each group has 'issue', 'count', and the full "
            "'rows' list of row indices with that issue. A typo repeated 40 times is "
            "one group with 40 rows, not 40 separate entries. 'total_violations' is "
            "the true total across all groups. When fixing or marking, use each "
            "group's full 'rows' list — never assume rows outside it are clean, and "
            "never skip rows because a group looks large; bulk_mark_errors takes a "
            "list, so pass all of a group's rows in one call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "column_name": {"type": "string", "description": "Column to validate."},
            },
            "required": ["column_name"],
        },
    },
    {
        "name": "fix_dtype",
        "description": (
            "Coerce a column to the correct dtype (int, float, str, date). "
            "Values that cannot be converted become NaN. "
            "After calling this, re-validate the column — any NaN that violates "
            "nullable=false should then be handled with mark_row_error."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "column_name": {"type": "string"},
                "target_dtype": {"type": "string", "enum": ["int", "float", "str", "date"]},
            },
            "required": ["column_name", "target_dtype"],
        },
    },
    {
        "name": "fix_allowed_values",
        "description": (
            "Replace disallowed values with their correct counterparts. "
            "Use for typos and casing errors where the correct value is obvious "
            "(e.g. 'Electronis' → 'Electronics', 'sports' → 'Sports'). "
            "Pass replacements as a dict mapping bad_value → correct_value."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "column_name": {"type": "string"},
                "replacements": {
                    "type": "object",
                    "description": "e.g. {\"Electronis\": \"Electronics\", \"sports\": \"Sports\"}",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["column_name", "replacements"],
        },
    },
    {
        "name": "fill_null",
        "description": (
            "Fill null values in a column with a known correct value. "
            "Only use when you are certain of the correct fill value. "
            "If you cannot determine the correct value, use mark_row_error instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "column_name": {"type": "string"},
                "fill_value": {"type": "string", "description": "The value to fill nulls with."},
            },
            "required": ["column_name", "fill_value"],
        },
    },
    {
        "name": "bulk_mark_errors",
        "description": (
            "Mark multiple rows with errors in a single call. "
            "Use this instead of calling mark_row_error repeatedly. "
            "Pass all unfixable violations for a column at once as a list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "violations": {
                    "type": "array",
                    "description": "List of violations to mark.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "row_index":   {"type": "integer"},
                            "column_name": {"type": "string"},
                            "issue":       {"type": "string", "description": "Plain-English sentence describing the problem."},
                            "id_column":   {"type": "string"},
                        },
                        "required": ["row_index", "column_name", "issue"],
                    },
                },
            },
            "required": ["violations"],
        },
    },
    {
        "name": "save_with_report",
        "description": (
            "Save two files: "
            "1. A CSV output (all rows with _errors column, or only clean rows if exclude_error_rows=true). "
            "2. A report CSV listing every flagged row, field by field. "
            "This will be REJECTED unless every schema column has a validate_column call "
            "(after this round's fixes/marks) showing 0 remaining violations — call "
            "validate_column one final time per column to confirm before calling this."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "csv_path": {"type": "string", "description": "Path for the output CSV."},
                "report_path": {"type": "string", "description": "Path for the violations report CSV."},
                "id_column": {
                    "type": "string",
                    "description": "Column to use as the row identifier in the report (e.g. 'product_id'). Leave blank to use row index.",
                },
                "exclude_error_rows": {
                    "type": "boolean",
                    "description": "If true, only rows with no errors are written to the output CSV. Use for strict/clean-only mode.",
                },
            },
            "required": ["csv_path", "report_path"],
            "cache_control": {"type": "ephemeral"},
        },
    },
]


def _friendly_call(name: str, inputs: dict) -> str:
    """Plain-English description of a tool call, for the live log."""
    if name == "inspect_data":
        return "Taking a first look at the data"
    if name == "validate_column":
        return f"Checking '{inputs.get('column_name')}' for problems"
    if name == "fix_dtype":
        return f"Converting '{inputs.get('column_name')}' values to {inputs.get('target_dtype')}"
    if name == "fix_allowed_values":
        n = len(inputs.get("replacements", {}))
        return f"Fixing {n} misspelled/mis-cased value(s) in '{inputs.get('column_name')}'"
    if name == "fill_null":
        return f"Filling missing values in '{inputs.get('column_name')}' with '{inputs.get('fill_value')}'"
    if name == "bulk_mark_errors":
        n = len(inputs.get("violations", []))
        return f"Flagging {n} row(s) that can't be safely auto-fixed"
    if name == "save_with_report":
        return "Saving the cleaned file and error report"
    return name


def _friendly_result(name: str, result_str: str) -> str:
    """Plain-English summary of a tool result, for the live log."""
    try:
        data = json.loads(result_str)
    except Exception:
        return result_str[:200]

    if isinstance(data, dict) and data.get("error"):
        return f"Could not complete that step: {data['error']}"

    if name == "inspect_data":
        rows = data["shape"]["rows"]
        cols = data["shape"]["columns"]
        return f"{rows:,} rows, {cols} columns detected"

    if name == "validate_column":
        col = data.get("column", "")
        total = data.get("total_violations", 0)
        groups = data.get("distinct_issue_groups", 0)
        if total == 0:
            return f"'{col}' is clean — no problems found"
        summary = f"Found {total:,} problem(s) in '{col}' ({groups} distinct type(s))"
        if data.get("note"):
            summary += " — more to review, will check again after handling these"
        return summary

    if name == "fix_dtype":
        ok = data.get("successfully_converted", 0)
        failed = data.get("failed_to_convert", 0)
        summary = f"Converted {ok:,} value(s)"
        if failed:
            summary += f", {failed:,} could not be converted and need review"
        return summary

    if name == "fix_allowed_values":
        n = data.get("total_cells_changed", 0)
        return f"Corrected {n:,} value(s)"

    if name == "fill_null":
        return data.get("message", "Done")

    if name == "bulk_mark_errors":
        n = data.get("marked", 0)
        return f"Flagged {n:,} row(s) for review"

    if name == "save_with_report":
        clean = data.get("clean_rows", 0)
        flagged = data.get("rows_with_errors", 0)
        return f"Saved — {clean:,} clean row(s), {flagged:,} flagged for review"

    return result_str[:200]


def dispatch_tool(name: str, inputs: dict, schema: DataSchema) -> str:
    try:
        if name == "inspect_data":
            return t.inspect_data()
        if name == "validate_column":
            return t.validate_column(inputs["column_name"], schema)
        if name == "fix_dtype":
            return t.fix_dtype(inputs["column_name"], inputs["target_dtype"])
        if name == "fix_allowed_values":
            return t.fix_allowed_values(inputs["column_name"], inputs["replacements"])
        if name == "fill_null":
            return t.fill_null(inputs["column_name"], inputs["fill_value"])
        if name == "bulk_mark_errors":
            return t.bulk_mark_errors(inputs["violations"])
        if name == "save_with_report":
            return t.save_with_report(
                inputs["csv_path"],
                inputs["report_path"],
                inputs.get("id_column", ""),
                inputs.get("exclude_error_rows", False),
                schema,
            )
        return json.dumps({"error": f"Unknown tool: {name}"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def run_agent(
    df: pd.DataFrame,
    schema: DataSchema,
    output_path: str = "data/clean.csv",
    mode: str = "clean",
    max_iterations: int = 80,
    on_log: Callable[[dict], None] | None = None,
) -> pd.DataFrame:
    """
    mode:
      "audit"  — validate only, never fix. Returns error report + annotated CSV.
      "clean"  — fix what's safe, mark the rest. Returns annotated CSV + report.
      "strict" — fix what's safe, mark the rest, then strip error rows from output CSV.
    """

    def _log(msg: dict) -> None:
        if on_log:
            on_log(msg)
            return
        t_type = msg.get("type")
        if t_type == "start":
            print("\n" + "=" * 60 + "\nAGENT STARTED\n" + "=" * 60 + "\n")
        elif t_type == "iteration":
            print(f"--- Iteration {msg['n']} ---")
        elif t_type == "claude_text":
            print(f"\nClaude: {msg['text']}\n")
        elif t_type == "tool_call":
            print(f"  > {msg['input_summary']}")
        elif t_type == "tool_result":
            print(f"  < {msg['preview']}\n")
        elif t_type == "finish":
            print("\n" + "=" * 60 + "\nAGENT FINISHED\n" + "=" * 60)
        elif t_type == "warning":
            print(f"\nWARNING: {msg['message']}")

    t.set_dataframe(df)
    client = anthropic.Anthropic()

    report_path = derive_report_path(output_path)

    _base = f"""You are a data-quality agent. The dataset must conform to this schema:

{schema.to_description()}
"""

    if mode == "audit":
        system_prompt = _base + f"""
MODE: Audit Only — inspect and report, do NOT modify any data.

WORKFLOW:
1. Call inspect_data.
2. Call validate_column for every column.
3. For EVERY violation, call mark_row_error — record it exactly as found.
   Do NOT call fix_dtype, fix_allowed_values, or fill_null under any circumstances.
4. After marking a column's violations, call validate_column on it one more time to
   confirm 0 remain — save_with_report will reject the save otherwise.
5. When every column shows 0 remaining violations, call save_with_report:
   - csv_path="{output_path}"
   - report_path="{report_path}"
   - id_column= column that best identifies a row (e.g. "product_id")
   - exclude_error_rows=false (keep all rows, just annotate them)

Be brief. One sentence of reasoning per step. When marking unfixable violations, always use bulk_mark_errors to handle all rows for a column in a single call — never call mark_row_error in a loop.
"""
    elif mode == "strict":
        system_prompt = _base + f"""
MODE: Strict Clean — fix what is safely fixable, drop everything else.

WORKFLOW:
1. Call inspect_data.
2. Call validate_column for every column.
3. For each violation:
   - Obvious typo/casing in allowed_values → fix_allowed_values
   - Wrong dtype that is clearly readable (e.g. a number stored as text) → fix_dtype, then re-validate
   - Anything ambiguous or unrecoverable → mark_row_error
   - Null in a nullable field → leave it alone
4. After fixing or marking a column's violations, call validate_column on it one more
   time to confirm 0 remain — save_with_report will reject the save otherwise.
5. When every column shows 0 remaining violations, call save_with_report:
   - csv_path="{output_path}"
   - report_path="{report_path}"
   - id_column= column that best identifies a row (e.g. "product_id")
   - exclude_error_rows=true  ← this removes flagged rows from the output CSV

Be brief. One sentence of reasoning per step. When marking unfixable violations, always use bulk_mark_errors to handle all rows for a column in a single call — never call mark_row_error in a loop.
"""
    else:  # "clean" — default
        system_prompt = _base + f"""
MODE: Clean + Report — fix what is safe, preserve everything else with error notes.

WORKFLOW:
1. Call inspect_data.
2. Call validate_column for every column.
3. For each violation:
   - Obvious typo/casing in allowed_values → fix_allowed_values
   - Wrong dtype that is clearly readable → fix_dtype, then re-validate
   - Unrecoverable value, unknown correct value, unfixable null → mark_row_error (row is kept)
   - Null in a nullable field → leave it alone
4. After fixing or marking a column's violations, call validate_column on it one more
   time to confirm 0 remain — save_with_report will reject the save otherwise.
5. When every column shows 0 remaining violations, call save_with_report:
   - csv_path="{output_path}"
   - report_path="{report_path}"
   - id_column= column that best identifies a row (e.g. "product_id")
   - exclude_error_rows=false (keep all rows)

Be brief. One sentence of reasoning per step. When marking unfixable violations, always use bulk_mark_errors to handle all rows for a column in a single call — never call mark_row_error in a loop.
"""

    messages: list[dict] = [
        {"role": "user", "content": "Please clean the dataset. Start now."}
    ]

    column_status: dict[str, str] = {c: "pending" for c in schema.columns}
    stats = {"fixed": 0, "flagged": 0}

    def _emit_progress() -> None:
        _log({"type": "progress", "columns": dict(column_status), **stats})

    _log({"type": "start"})
    _emit_progress()

    for iteration in range(max_iterations):
        _log({"type": "iteration", "n": iteration + 1})

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        agent_text, tool_calls_made = [], []
        for block in response.content:
            if block.type == "text":
                agent_text.append(block.text)
            elif block.type == "tool_use":
                tool_calls_made.append(block)

        if agent_text:
            _log({"type": "claude_text", "text": " ".join(agent_text)})

        if tool_calls_made:
            tool_results = []
            for tb in tool_calls_made:
                input_summary = _friendly_call(tb.name, tb.input)
                _log({"type": "tool_call", "name": tb.name, "input_summary": input_summary})

                result_str = dispatch_tool(tb.name, tb.input, schema)

                # Backstop only — validate_column now self-limits its own payload size,
                # so this should rarely trigger in normal operation.
                MAX_RESULT = 4000
                claude_result = (
                    result_str[:MAX_RESULT] + f"\n...truncated ({len(result_str)} chars total)"
                    if len(result_str) > MAX_RESULT else result_str
                )

                preview = _friendly_result(tb.name, result_str)
                _log({"type": "tool_result", "preview": preview})

                try:
                    parsed = json.loads(result_str)
                except Exception:
                    parsed = {}

                if tb.name == "validate_column" and isinstance(parsed, dict) and parsed.get("column") in column_status:
                    col = parsed["column"]
                    column_status[col] = "clean" if parsed.get("total_violations", 0) == 0 else "checking"
                    _emit_progress()
                elif tb.name == "fix_allowed_values" and isinstance(parsed, dict):
                    stats["fixed"] += parsed.get("total_cells_changed", 0)
                    _emit_progress()
                elif tb.name == "fill_null" and isinstance(parsed, dict):
                    stats["fixed"] += parsed.get("affected", 0)
                    _emit_progress()
                elif tb.name == "bulk_mark_errors" and isinstance(parsed, dict):
                    stats["flagged"] += parsed.get("marked", 0)
                    _emit_progress()
                elif tb.name == "save_with_report" and isinstance(parsed, dict) and "error" not in parsed:
                    _log({"type": "summary", **parsed})

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tb.id,
                    "content": claude_result,
                })

            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue

        if response.stop_reason == "end_turn":
            _log({"type": "finish"})
            break
    else:
        _log({"type": "warning", "message": f"Reached max_iterations ({max_iterations})."})

    return t.get_dataframe()
