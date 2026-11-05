"""The real app with a scripted agent, so browser tests are fast, free and repeatable.

Only the Claude loop is replaced. Uploads, schema building, validation, saving and the
review all run the real code. Started by playwright.config.ts; never used in production.
"""
import json
import sys
import time
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as web  # noqa: E402
import tools  # noqa: E402
from agent import derive_report_path  # noqa: E402

PORT = 8010
STEP_SECONDS = 0.1


def fake_run_agent(df, schema, output_path, mode, on_log):
    """Flags every schema violation, then saves, sending the events a real run sends."""
    tools.set_dataframe(df)
    columns = list(schema.columns)
    on_log({"type": "start"})

    flagged = 0
    for step, column in enumerate(columns, start=1):
        time.sleep(STEP_SECONDS)
        on_log({"type": "iteration", "n": step})
        on_log({"type": "tool_call", "name": "mark_remaining_violations", "input_summary": f"Checking {column}"})
        flagged += json.loads(tools.mark_remaining_violations(column, schema))["marked"]
        status = {name: "clean" if i < step else "pending" for i, name in enumerate(columns)}
        on_log({"type": "progress", "columns": status, "fixed": 0, "flagged": flagged})

    saved = tools.save_with_report(
        output_path,
        derive_report_path(output_path),
        exclude_error_rows=mode == "strict",
    )
    on_log({"type": "summary", **json.loads(saved)})
    on_log({"type": "finish"})


web.run_agent = fake_run_agent

if __name__ == "__main__":
    uvicorn.run(web.app, host="127.0.0.1", port=PORT, log_level="warning")
