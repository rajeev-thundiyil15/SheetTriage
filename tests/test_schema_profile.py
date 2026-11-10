"""Tests for the file scan, its merge with Claude's reply, and date_format. No API calls."""
import json

import pandas as pd
import pytest

import tools as t
from schema import ColumnSchema, DataSchema
from schema_infer import infer_schema, merge_rules
from schema_profile import MAX_QUESTIONS, build_questions


def scan(**columns) -> dict:
    return build_questions(pd.DataFrame(columns, dtype=str))


def question(draft: dict, question_id: str) -> dict:
    return next(q for q in draft["questions"] if q["id"] == question_id)


# ---------------------------------------------------------------------------
# Dates: only ask when a wrong guess could change the data
# ---------------------------------------------------------------------------

def test_ambiguous_dates_must_be_answered_with_no_suggestion():
    draft = scan(signed=["03/04/2024", "05/06/2024", "01/02/2024"])
    q = question(draft, "date_format:signed")

    assert q["required"] is True
    assert q["recommended"] is None
    assert "03/04/2024" in q["question"]
    assert "date_format" not in draft["schema"]["columns"]["signed"]


def test_a_day_above_12_settles_day_first_without_asking():
    draft = scan(signed=["25/04/2024", "03/04/2024"])

    assert draft["schema"]["columns"]["signed"]["date_format"] == "DMY"
    assert not draft["questions"]
    assert "day first" in draft["decisions"][0]["decision"]


def test_a_middle_number_above_12_settles_month_first_without_asking():
    draft = scan(signed=["04/25/2024", "03/04/2024"])

    assert draft["schema"]["columns"]["signed"]["date_format"] == "MDY"
    assert not draft["questions"]


def test_an_even_mix_of_orders_is_asked_about_with_no_suggestion():
    draft = scan(signed=["25/04/2024", "04/25/2024"])
    q = question(draft, "date_format:signed")

    assert q["recommended"] is None
    assert "1 date(s) only work month first" in q["why"]
    assert "1 only work day first" in q["why"]


def test_when_one_order_clearly_wins_it_is_suggested():
    # The 30,000-row test: 99% month first, but nothing was suggested and day first flagged 17,018 good dates.
    draft = scan(signed=[f"02/{d}/2026" for d in range(13, 29)] * 10 + ["13/02/2026"])
    q = question(draft, "date_format:signed")

    assert q["required"] is True
    assert q["options"][q["recommended"]]["patch"] == {"date_format": "MDY"}
    assert "160 date(s) only work month first" in q["why"]
    assert "Most dates are month first" in q["why"]
    assert draft["schema"]["columns"]["signed"]["date_format"] == "MDY"


def test_a_clear_day_first_majority_is_suggested_too():
    draft = scan(signed=[f"{d}/02/2026" for d in range(13, 29)] * 10 + ["02/13/2026"])

    assert draft["schema"]["columns"]["signed"]["date_format"] == "DMY"


# ---------------------------------------------------------------------------
# Numbers written as decimals, and money columns
# ---------------------------------------------------------------------------

def test_numbers_written_with_decimals_are_drafted_as_decimals():
    assert scan(fee_total=["45.00", "20.00", "180.00"])["schema"]["columns"]["fee_total"]["dtype"] == "float"


def test_money_columns_are_decimals_even_when_whole():
    assert scan(price=["45", "20", "180"])["schema"]["columns"]["price"]["dtype"] == "float"


def test_whole_numbers_stay_whole():
    assert scan(quantity=["1", "2", "3"])["schema"]["columns"]["quantity"]["dtype"] == "int"
    assert scan(order_count=["1", "2"])["schema"]["columns"]["order_count"]["dtype"] == "int"


def test_iso_dates_are_decided_and_bad_values_reported():
    draft = scan(last_restocked=["2024-11-03"] * 9 + ["not-a-date"])
    column = draft["schema"]["columns"]["last_restocked"]

    assert column["dtype"] == "date"
    assert column["date_format"] == "YMD"
    assert any("1 value(s) that aren't dates" in d["decision"] for d in draft["decisions"])


# ---------------------------------------------------------------------------
# Other findings
# ---------------------------------------------------------------------------

def test_near_duplicates_become_an_allowed_values_question():
    draft = scan(category=["Electronics"] * 20 + ["Electronis"] * 5 + ["Sports"] * 20 + ["sports"] * 3)
    q = question(draft, "allowed:category")

    assert q["options"][0]["patch"]["allowed_values"] == ["Electronics", "Sports"]
    assert "'Electronis' looks like 'Electronics'" in q["why"]
    assert draft["schema"]["columns"]["category"]["allowed_values"] == ["Electronics", "Sports"]


def test_rare_values_are_left_out_of_the_suggested_list():
    # The 30,000-row test offered 'U.S.' and 'Untied Kingdom' as allowed countries.
    draft = scan(country=["USA"] * 450 + ["UK"] * 150 + ["Canada"] * 150 + ["usa"] * 4 + ["U.S."] * 5 + ["Untied Kingdom"] * 4)
    q = question(draft, "allowed:country")

    assert q["options"][0]["patch"]["allowed_values"] == ["USA", "UK", "Canada"]
    assert "Left out because they're rare: 'Untied Kingdom' (4), 'U.S.' (5)" in q["why"]
    assert q["options"][2]["patch"]["allowed_values"] == ["USA", "UK", "Canada", "Untied Kingdom", "U.S."]
    assert draft["schema"]["columns"]["country"]["allowed_values"] == ["USA", "UK", "Canada"]


def test_a_value_seen_once_is_rare_when_the_others_repeat():
    draft = scan(ticket=["General"] * 9 + ["VIP"] * 8 + ["Student"] * 7 + ["Early Bird"])

    assert question(draft, "allowed:ticket")["options"][0]["patch"]["allowed_values"] == ["General", "VIP", "Student"]


def test_an_uncommon_but_real_value_is_kept():
    draft = scan(ticket=["General"] * 60 + ["Student"] * 20 + ["VIP"] * 15 + ["Speaker"] * 5)

    assert "Speaker" in question(draft, "allowed:ticket")["options"][0]["patch"]["allowed_values"]
    assert len(question(draft, "allowed:ticket")["options"]) == 2


def test_two_common_similar_values_are_not_merged():
    draft = scan(status=["Female"] * 10 + ["Male"] * 10)

    assert question(draft, "allowed:status")["options"][0]["patch"]["allowed_values"] == ["Female", "Male"]


@pytest.mark.parametrize("name", ["notes", "Order Notes", "customer_comment", "Description"])
def test_free_text_columns_default_to_any_text(name):
    draft = scan(**{name: ["Late"] * 15 + ["Ok"] * 15})

    assert question(draft, f"allowed:{name}")["recommended"] == 1
    assert draft["schema"]["columns"][name].get("allowed_values") is None


def test_a_name_that_only_contains_a_free_text_word_keeps_the_list():
    assert question(scan(denotes=["a"] * 15 + ["b"] * 15), "allowed:denotes")["recommended"] == 0


def test_free_text_is_not_treated_as_a_list():
    draft = scan(note=[f"note {i}" for i in range(30)])

    assert "allowed_values" not in draft["schema"]["columns"]["note"]
    assert not draft["questions"]


def test_mostly_numeric_column_is_asked_about():
    draft = scan(qty=["1", "2", "3", "4", "n/a"])

    assert "'n/a'" in question(draft, "not_number:qty")["question"]
    assert draft["schema"]["columns"]["qty"]["dtype"] == "int"


def test_all_numbers_are_decided():
    draft = scan(price=["1.5", "2"], qty=["1", "2"])

    assert draft["schema"]["columns"]["price"]["dtype"] == "float"
    assert draft["schema"]["columns"]["qty"]["dtype"] == "int"
    assert not draft["questions"]


def test_a_few_negatives_suggest_a_minimum_of_zero():
    draft = scan(price=[str(i) for i in range(1, 10)] + ["-5"])

    assert question(draft, "negative:price")["recommended"] == 0
    assert draft["schema"]["columns"]["price"]["min_value"] == 0


@pytest.mark.parametrize("blanks, nullable", [(1, False), (10, True)])
def test_blank_suggestion_depends_on_how_many_there_are(blanks, nullable):
    draft = scan(note=[f"note {i}" for i in range(100 - blanks)] + [None] * blanks)

    assert "blank:note" in {q["id"] for q in draft["questions"]}
    assert draft["schema"]["columns"]["note"]["nullable"] is nullable


def test_every_option_produces_a_valid_schema():
    draft = scan(
        signed=["03/04/2024"] * 30,
        category=["A"] * 15 + ["B"] * 15,
        qty=["1"] * 29 + ["n/a"],
        price=["-1"] + ["2"] * 29,
        note=["x"] * 29 + [None],
    )
    assert draft["questions"]
    for q in draft["questions"]:
        for option in q["options"]:
            column = dict(draft["schema"]["columns"][q["column"]])
            column.update({k: v for k, v in option["patch"].items() if v is not None})
            ColumnSchema.model_validate(column)


def test_questions_are_capped_and_the_rest_become_stated_decisions():
    draft = scan(**{f"note{i}": ["x"] * 29 + [None] for i in range(MAX_QUESTIONS + 2)})

    assert len(draft["questions"]) == MAX_QUESTIONS
    assert sum("Can note" in d["reason"] for d in draft["decisions"]) == 2


def test_required_questions_are_never_dropped_by_the_cap():
    draft = scan(**{f"date{i}": ["03/04/2024"] * 3 for i in range(MAX_QUESTIONS + 2)})

    assert len(draft["questions"]) == MAX_QUESTIONS + 2


def test_scan_of_the_test_products_shape_builds_a_valid_schema():
    draft = scan(
        product_id=[str(i) for i in range(40)],
        category=["Books"] * 20 + ["Bookz"] * 2 + ["Food"] * 18,
        supplier_email=["a@b.co"] * 40,
        last_restocked=["2024-11-03"] * 40,
    )

    DataSchema.model_validate(draft["schema"])


# ---------------------------------------------------------------------------
# Merging Claude's reply
# ---------------------------------------------------------------------------

def test_no_rules_means_no_api_call(monkeypatch):
    def fail():
        raise AssertionError("should not call the API")

    monkeypatch.setattr("schema_infer.anthropic.Anthropic", fail)

    assert infer_schema(pd.DataFrame({"a": ["1"]}, dtype=str))["schema"]["columns"]["a"]["dtype"] == "int"


def test_rules_change_columns_and_remove_answered_questions():
    draft = scan(signed=["03/04/2024"] * 3)

    merge_rules(draft, {
        "column_changes": {"signed": {"date_format": "DMY"}},
        "answered_question_ids": ["date_format:signed"],
        "decisions": [{"column": "signed", "decision": "Read as day first", "reason": "Your rule"}],
    })

    assert draft["schema"]["columns"]["signed"]["date_format"] == "DMY"
    assert not draft["questions"]
    assert draft["decisions"][0]["reason"] == "Your rule"


@pytest.mark.parametrize("changes", [
    {"date_format": "YYYY"},
    {"dtype": "money"},
    {"made_up_field": 1},
])
def test_invalid_changes_from_claude_are_ignored(changes):
    draft = scan(qty=["1"])

    merge_rules(draft, {"column_changes": {"qty": changes}})

    assert draft["schema"]["columns"]["qty"] == {"nullable": False, "dtype": "int"}


def test_changes_to_unknown_columns_are_ignored():
    draft = scan(qty=["1"])
    merge_rules(draft, {"column_changes": {"ghost": {"dtype": "int"}}})

    assert "ghost" not in draft["schema"]["columns"]


def test_claude_can_add_a_question():
    draft = scan(qty=["1"])
    merge_rules(draft, {"questions": [{
        "column": "qty",
        "question": "What is the highest valid quantity?",
        "why": "Your rule didn't give a limit.",
        "options": [
            {"label": "100", "patch": {"max_value": 100}},
            {"label": "No limit", "patch": {"max_value": None}},
        ],
        "recommended": 1,
    }]})

    assert draft["questions"][0]["recommended"] == 1
    assert "max_value" not in draft["schema"]["columns"]["qty"]


def test_claude_cannot_repeat_a_question_the_scan_asked():
    draft = scan(signed=["03/04/2024"] * 10, price=["-1"] + ["2"] * 9)
    before = len(draft["questions"])

    merge_rules(draft, {"questions": [
        {
            "column": "signed",
            "question": "Is the date format MM/DD/YYYY or DD/MM/YYYY?",
            "options": [
                {"label": "Month first", "patch": {"date_format": "MDY"}},
                {"label": "Day first", "patch": {"date_format": "DMY"}},
            ],
        },
        {
            "column": "price",
            "question": "Should negative prices be rejected?",
            "options": [
                {"label": "Yes", "patch": {"min_value": 0}},
                {"label": "No", "patch": {"min_value": None}},
            ],
        },
    ]})

    assert len(draft["questions"]) == before


def test_a_question_stays_unless_the_rules_set_what_it_asks_about():
    draft = scan(price=["-1"] + ["2"] * 9)

    merge_rules(draft, {
        "column_changes": {"price": {"max_value": 150}},
        "answered_question_ids": ["negative:price"],
    })

    assert [q["id"] for q in draft["questions"]] == ["negative:price"]
    assert draft["schema"]["columns"]["price"]["max_value"] == 150


def test_a_question_goes_when_the_rules_set_what_it_asks_about():
    draft = scan(price=["-1"] + ["2"] * 9)

    merge_rules(draft, {
        "column_changes": {"price": {"min_value": -100}},
        "answered_question_ids": ["negative:price"],
    })

    assert not draft["questions"]
    assert draft["schema"]["columns"]["price"]["min_value"] == -100


def test_a_bad_question_from_claude_is_dropped():
    draft = scan(qty=["1"])
    merge_rules(draft, {"questions": [
        {"column": "ghost", "options": [{"label": "a", "patch": {}}, {"label": "b", "patch": {}}]},
        {"column": "qty", "options": [{"label": "only one", "patch": {}}]},
        {"column": "qty", "options": [{"label": "a", "patch": {"dtype": "money"}}, {"label": "b", "patch": {}}]},
    ]})

    assert not draft["questions"]


# ---------------------------------------------------------------------------
# date_format in validation and fixes
# ---------------------------------------------------------------------------

def date_schema(date_format=None) -> DataSchema:
    return DataSchema(name="t", columns={"d": ColumnSchema(dtype="date", date_format=date_format)})


def load_dates(*values) -> None:
    t.set_dataframe(pd.DataFrame({"d": list(values)}))


@pytest.mark.parametrize("date_format, expected", [("DMY", "2024-04-03"), ("MDY", "2024-03-04")])
def test_chosen_format_decides_how_a_date_reads(date_format, expected):
    load_dates("03/04/2024")

    result = json.loads(t.fix_dtype("d", "date", date_format))

    assert result["failed_to_convert"] == 0
    assert t.get_dataframe()["d"].dt.strftime("%Y-%m-%d").tolist() == [expected]


def test_without_a_format_ambiguous_dates_are_flagged_and_kept():
    load_dates("03/04/2024", "25/04/2024")
    schema = date_schema()

    assert json.loads(t.fix_dtype("d", "date"))["failed_to_convert"] == 1
    result = json.loads(t.validate_column("d", schema))

    assert t.get_dataframe()["d"].tolist() == ["03/04/2024", "2024-04-25"]
    assert result["total_violations"] == 1
    assert "day first or month first" in result["groups"][0]["issue"]


def test_chosen_format_rejects_a_date_that_cannot_fit_it():
    load_dates("25/04/2024")

    assert json.loads(t.validate_column("d", date_schema("MDY")))["total_violations"] == 1


def test_same_day_and_month_is_not_ambiguous():
    load_dates("03/03/2024")

    assert json.loads(t.validate_column("d", date_schema()))["total_violations"] == 0


def test_iso_dates_pass_whatever_format_is_chosen():
    load_dates("2024-11-03")

    assert json.loads(t.validate_column("d", date_schema("DMY")))["total_violations"] == 0


def test_two_digit_years():
    load_dates("03/04/24")
    t.fix_dtype("d", "date", "DMY")

    assert t.get_dataframe()["d"].dt.year.tolist() == [2024]


def test_dispatch_passes_the_schema_date_format():
    from agent import dispatch_tool

    load_dates("03/04/2024")
    dispatch_tool("fix_dtype", {"column_name": "d", "target_dtype": "date"}, date_schema("MDY"))

    assert t.get_dataframe()["d"].dt.month.tolist() == [3]
