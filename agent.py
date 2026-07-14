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
            "Validate one column against the schema. Returns every row that violates "
            "a rule, with the row index and a description of what is wrong."
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
        "name": "mark_row_error",
        "description": (
            "Flag a row as having an unfixable violation. The row is KEPT in the dataset "
            "with an _errors column noting what is wrong. Use this instead of dropping rows "
            "whenever you cannot safely infer the correct value. "
            "Examples: null required field, negative value with no correct substitute, "
            "invalid email format, value too far out of range to correct."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "row_index": {"type": "integer", "description": "Integer row index from validate_column output."},
                "column_name": {"type": "string", "description": "The column that has the violation."},
                "issue": {"type": "string", "description": "A complete, plain-English sentence describing the problem so a non-technical person can understand it. Include the field name and the actual value. Example: 'The email address \"bob-no-at\" is not valid — it must contain @ and a domain.' NOT: 'does not match pattern'."},
                "id_column": {
                    "type": "string",
                    "description": "Name of the column that identifies this row (e.g. 'product_id'). Used in the report.",
                },
            },
            "required": ["row_index", "column_name", "issue"],
        },
    },
    {
        "name": "save_with_report",
        "description": (
            "Save two files when ALL columns have been validated: "
            "1. A CSV output (all rows with _errors column, or only clean rows if exclude_error_rows=true). "
            "2. A report CSV listing every flagged row, field by field. "
            "Call this only once, at the very end."
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


def dispatch_tool(name: str, inputs: dict, schema: DataSchema) -> str:
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
    if name == "mark_row_error":
        return t.mark_row_error(
            inputs["row_index"],
            inputs["column_name"],
            inputs["issue"],
            inputs.get("id_column", ""),
        )
    if name == "save_with_report":
        return t.save_with_report(
            inputs["csv_path"],
            inputs["report_path"],
            inputs.get("id_column", ""),
            inputs.get("exclude_error_rows", False),
        )
    return json.dumps({"error": f"Unknown tool: {name}"})


def run_agent(
    df: pd.DataFrame,
    schema: DataSchema,
    output_path: str = "data/clean.csv",
    mode: str = "clean",
    max_iterations: int = 40,
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
            print(f"  [TOOL CALL] {msg['name']}({msg['input_summary']})")
        elif t_type == "tool_result":
            print(f"  [TOOL RESULT] {msg['preview']}\n")
        elif t_type == "finish":
            print("\n" + "=" * 60 + "\nAGENT FINISHED\n" + "=" * 60)
        elif t_type == "warning":
            print(f"\nWARNING: {msg['message']}")

    t.set_dataframe(df)
    client = anthropic.Anthropic()

    # Derive report path from output path: data/abc_clean.csv → data/abc_report.csv
    report_path = output_path.replace("_clean.csv", "_report.csv").replace(".csv", "_report.csv")

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
4. When all columns are validated, call save_with_report:
   - csv_path="{output_path}"
   - report_path="{report_path}"
   - id_column= column that best identifies a row (e.g. "product_id")
   - exclude_error_rows=false (keep all rows, just annotate them)

Be brief. One sentence of reasoning per step.
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
4. Re-validate after each fix to confirm it worked.
5. When all columns are done, call save_with_report:
   - csv_path="{output_path}"
   - report_path="{report_path}"
   - id_column= column that best identifies a row (e.g. "product_id")
   - exclude_error_rows=true  ← this removes flagged rows from the output CSV

Be brief. One sentence of reasoning per step.
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
4. Re-validate after each fix to confirm it worked.
5. When all columns are done, call save_with_report:
   - csv_path="{output_path}"
   - report_path="{report_path}"
   - id_column= column that best identifies a row (e.g. "product_id")
   - exclude_error_rows=false (keep all rows)

Be brief. One sentence of reasoning per step.
"""

    messages: list[dict] = [
        {"role": "user", "content": "Please clean the dataset. Start now."}
    ]

    _log({"type": "start"})

    for iteration in range(max_iterations):
        _log({"type": "iteration", "n": iteration + 1})

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
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
                input_summary = json.dumps(tb.input)[:120]
                _log({"type": "tool_call", "name": tb.name, "input_summary": input_summary})

                result_str = dispatch_tool(tb.name, tb.input, schema)

                MAX_RESULT = 1500
                claude_result = (
                    result_str[:MAX_RESULT] + f"\n...truncated ({len(result_str)} chars total)"
                    if len(result_str) > MAX_RESULT else result_str
                )

                preview = result_str[:200] + "..." if len(result_str) > 200 else result_str
                _log({"type": "tool_result", "preview": preview})

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
