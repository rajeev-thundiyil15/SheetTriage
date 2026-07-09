"""
schema_infer.py — converts natural language rules into DataSchema JSON.

Single cheap Claude call. Not an agent loop — just structured output.
"""
from __future__ import annotations

import json
import anthropic


def infer_schema(column_names: list[str], rules: str, sample_rows: list[dict] | None = None) -> dict:
    """
    Parameters
    ----------
    column_names  : list of column headers from the uploaded CSV
    rules         : plain-English rules from the user
    sample_rows   : first few rows of the CSV so Claude can infer dtypes

    Returns
    -------
    A dict matching the DataSchema format (ready for DataSchema.model_validate)
    """
    client = anthropic.Anthropic()

    columns_str = ", ".join(column_names)
    sample_str = ""
    if sample_rows:
        sample_str = f"\n\nSample data (first 3 rows):\n{json.dumps(sample_rows, default=str, indent=2)}"

    prompt = f"""You are a data schema generator.

CSV columns: {columns_str}{sample_str}

User rules:
\"\"\"{rules}\"\"\"

Convert the user's rules into a DataSchema JSON object. Return ONLY raw JSON — no markdown, no explanation, no code fences.

Required format:
{{
  "name": "dataset",
  "columns": {{
    "<column_name>": {{
      "dtype": "<int|float|str|date>",
      "nullable": <true|false>
    }}
  }}
}}

Additional optional fields per column (only include when relevant):
- "min_value": number
- "max_value": number
- "allowed_values": ["Value1", "Value2"]
- "pattern": "regex string"

Rules for inference:
- Every column in the CSV must appear in the output
- Infer dtype from the sample data and column name when the user doesn't specify
- nullable defaults to false (required) unless the user says "optional", "can be empty", or similar
- For email-like columns use pattern ".+@.+\\..+" unless told otherwise
- For date/time columns use dtype "date"
- For ID columns use dtype "int"
- Use allowed_values only when the user explicitly lists valid options
- Pattern for email: ".+@.+\\..+"
"""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = response.content[0].text.strip()

    # Strip accidental markdown fences
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    return json.loads(raw)
