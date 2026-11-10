"""Tests for agent.py helpers. No API calls."""
from types import SimpleNamespace

import pandas as pd
import pytest

import agent
from agent import derive_report_path, run_agent
from schema import ColumnSchema, DataSchema


@pytest.mark.parametrize("output_path, expected", [
    ("data/abc_clean.csv", "data/abc_report.csv"),
    ("data/abc.csv", "data/abc_report.csv"),
    ("abc_clean.csv", "abc_report.csv"),
    ("data/clean.csv", "data/clean_report.csv"),
    ("data/my_clean_data.csv", "data/my_clean_data_report.csv"),
    ("data/abc", "data/abc_report.csv"),
])
def test_derive_report_path(output_path, expected):
    assert derive_report_path(output_path) == expected


def test_derive_report_path_never_doubles_the_suffix():
    assert "_report_report" not in derive_report_path("data/abc_clean.csv")


# ---------------------------------------------------------------------------
# run_agent, driven by a scripted client instead of the API
# ---------------------------------------------------------------------------

SCHEMA = DataSchema(name="test", columns={"a": ColumnSchema(dtype="int")})


def tool(name, **inputs):
    return SimpleNamespace(type="tool_use", id=f"id_{name}", name=name, input=inputs)


def turn(*blocks, stop_reason="tool_use"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def stop(text="All done."):
    return turn(SimpleNamespace(type="text", text=text), stop_reason="end_turn")


def script(monkeypatch, responses):
    remaining = iter(responses)
    calls = []

    def create(**kwargs):
        calls.append(kwargs["messages"][-1])
        return next(remaining)

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(agent.anthropic, "Anthropic", lambda: client)
    return calls


def run(tmp_path, values=("x", "1")):
    events = []
    output = tmp_path / "out_clean.csv"
    run_agent(pd.DataFrame({"a": list(values)}), SCHEMA, output_path=str(output), on_log=events.append)
    return output, [e["type"] for e in events]


def save_call(tmp_path):
    return tool(
        "save_with_report",
        csv_path=str(tmp_path / "out_clean.csv"),
        report_path=str(tmp_path / "out_report.csv"),
    )


def test_run_marks_validates_and_saves(tmp_path, monkeypatch):
    script(monkeypatch, [
        turn(tool("validate_column", column_name="a")),
        turn(tool("mark_remaining_violations", column_name="a")),
        turn(tool("validate_column", column_name="a")),
        turn(save_call(tmp_path)),
        stop(),
    ])

    output, types = run(tmp_path)

    assert output.exists()
    assert types[-1] == "finish"
    assert len(pd.read_csv(output)) == 2


def test_stopping_before_saving_gets_a_reminder_and_can_recover(tmp_path, monkeypatch):
    calls = script(monkeypatch, [
        stop("Here is a summary instead of a file."),
        turn(tool("validate_column", column_name="a")),
        turn(tool("mark_remaining_violations", column_name="a")),
        turn(tool("validate_column", column_name="a")),
        turn(save_call(tmp_path)),
        stop(),
    ])

    output, types = run(tmp_path)

    assert "Nothing has been saved" in calls[1]["content"]
    assert output.exists()
    assert types[-1] == "finish"


def test_never_saving_raises_instead_of_reporting_success(tmp_path, monkeypatch):
    script(monkeypatch, [stop() for _ in range(agent.MAX_SAVE_REMINDERS + 1)])
    events = []

    with pytest.raises(RuntimeError, match="no file was produced"):
        run_agent(
            pd.DataFrame({"a": ["1"]}),
            SCHEMA,
            output_path=str(tmp_path / "out_clean.csv"),
            on_log=events.append,
        )

    assert "finish" not in [e["type"] for e in events]


def test_gate_refusal_does_not_count_as_saved(tmp_path, monkeypatch):
    script(monkeypatch, [turn(save_call(tmp_path))] + [stop() for _ in range(agent.MAX_SAVE_REMINDERS + 1)])

    with pytest.raises(RuntimeError):
        run(tmp_path)

    assert not (tmp_path / "out_clean.csv").exists()


def test_running_out_of_iterations_raises(tmp_path, monkeypatch):
    script(monkeypatch, [turn(tool("inspect_data")) for _ in range(3)])

    with pytest.raises(RuntimeError):
        run_agent(pd.DataFrame({"a": ["1"]}), SCHEMA, output_path=str(tmp_path / "o.csv"), max_iterations=3)


@pytest.mark.parametrize("raw, plain", [
    ("✅ **Cleaning complete!**", "Cleaning complete!"),
    ("**Summary:**\n- **Input rows:** 30,000  \n- **Clean rows:** 27,957", "Summary:\n- Input rows: 30,000\n- Clean rows: 27,957"),
    ("## Results\n* `price`: 459 flagged", "Results\n- price: 459 flagged"),
    ("Fixed 'Genral' → 'General' in ticket_type.", "Fixed 'Genral' → 'General' in ticket_type."),
    ("Price is 2 * 3 = 6", "Price is 2 * 3 = 6"),
    ("Done 🎉 all saved", "Done all saved"),
])
def test_agent_text_is_shown_without_markdown_or_emoji(raw, plain):
    from agent import plain_text

    assert plain_text(raw) == plain
