"""Tests for src.ai.intent_parser (Bedrock calls mocked)."""

from unittest.mock import MagicMock, patch

import pytest

from src.ai.intent_parser import (
    IntentParseError,
    parse_health_intent,
)
from src.query.monthly_report import (
    default_report_month,
)


def _parse(tool_input: dict, text: str = "watchcon-a 상태 확인해줘"):
    with patch(
        "src.ai.intent_parser.invoke_tool",
        return_value=tool_input,
    ) as mocked:
        result = parse_health_intent(
            bedrock_session=MagicMock(),
            text=text,
        )
    return result, mocked


# --- empty input short-circuits without calling Bedrock -----------------


def test_parse_health_intent_rejects_blank_text_without_calling_bedrock():
    with patch(
        "src.ai.intent_parser.invoke_tool"
    ) as mocked:
        with pytest.raises(IntentParseError):
            parse_health_intent(
                bedrock_session=MagicMock(),
                text="   ",
            )

    mocked.assert_not_called()


# --- target extraction -------------------------------------------------


def test_parse_health_intent_rejects_a_missing_target():
    with pytest.raises(IntentParseError):
        _parse(
            {"mode": "live"}
        )


def test_parse_health_intent_rejects_a_blank_target():
    with pytest.raises(IntentParseError):
        _parse(
            {"target": "   ", "mode": "live"}
        )


def test_parse_health_intent_strips_the_target():
    (target, _, _), _ = _parse(
        {
            "target": "  watchcon-a  ",
            "mode": "live",
        }
    )

    assert target == "watchcon-a"


# --- mode: live (default) -----------------------------------------------


def test_parse_health_intent_defaults_to_live_for_an_unrecognized_mode():
    (target, mode, value), _ = _parse(
        {"target": "watchcon-a"}
    )

    assert (target, mode, value) == (
        "watchcon-a",
        "live",
        "30",
    )


def test_parse_health_intent_uses_the_given_live_lookback():
    (_, mode, value), _ = _parse(
        {
            "target": "watchcon-a",
            "mode": "live",
            "lookback_minutes": 45,
        }
    )

    assert (mode, value) == ("live", "45")


def test_parse_health_intent_rejects_a_non_positive_live_lookback():
    (_, _, value), _ = _parse(
        {
            "target": "watchcon-a",
            "mode": "live",
            "lookback_minutes": 0,
        }
    )

    assert value == "30"


def test_parse_health_intent_rejects_a_non_integer_live_lookback():
    (_, _, value), _ = _parse(
        {
            "target": "watchcon-a",
            "mode": "live",
            "lookback_minutes": "45",
        }
    )

    assert value == "30"


# --- mode: date -----------------------------------------------------


def test_parse_health_intent_returns_the_given_date():
    (target, mode, value), _ = _parse(
        {
            "target": "watchcon-a",
            "mode": "date",
            "date": "2026-08-19",
        }
    )

    assert (target, mode, value) == (
        "watchcon-a",
        "date",
        "2026-08-19",
    )


def test_parse_health_intent_rejects_a_missing_date():
    with pytest.raises(IntentParseError):
        _parse(
            {
                "target": "watchcon-a",
                "mode": "date",
            }
        )


# --- mode: storage -----------------------------------------------------


def test_parse_health_intent_uses_the_given_storage_lookback():
    (_, mode, value), _ = _parse(
        {
            "target": "watchcon-a",
            "mode": "storage",
            "lookback_days": 14,
        }
    )

    assert (mode, value) == ("storage", "14")


def test_parse_health_intent_defaults_storage_lookback_to_30_days():
    (_, mode, value), _ = _parse(
        {
            "target": "watchcon-a",
            "mode": "storage",
        }
    )

    assert (mode, value) == ("storage", "30")


def test_parse_health_intent_rejects_a_negative_storage_lookback():
    (_, _, value), _ = _parse(
        {
            "target": "watchcon-a",
            "mode": "storage",
            "lookback_days": -5,
        }
    )

    assert value == "30"


# --- mode: topsql -----------------------------------------------------


def test_parse_health_intent_uses_the_given_topsql_lookback():
    (_, mode, value), _ = _parse(
        {
            "target": "watchcon-a",
            "mode": "topsql",
            "lookback_minutes": 5,
        }
    )

    assert (mode, value) == ("topsql", "5")


def test_parse_health_intent_defaults_topsql_lookback_to_10_minutes():
    (_, mode, value), _ = _parse(
        {
            "target": "watchcon-a",
            "mode": "topsql",
        }
    )

    assert (mode, value) == ("topsql", "10")


# --- mode: report -----------------------------------------------------


def test_parse_health_intent_returns_the_given_report_month():
    (target, mode, value), _ = _parse(
        {
            "target": "watchcon-a",
            "mode": "report",
            "month": "2026-07",
        }
    )

    assert (target, mode, value) == (
        "watchcon-a",
        "report",
        "2026-07",
    )


def test_parse_health_intent_defaults_a_missing_report_month():
    (_, mode, value), _ = _parse(
        {
            "target": "watchcon-a",
            "mode": "report",
        }
    )

    assert mode == "report"
    assert value == default_report_month()


def test_parse_health_intent_defaults_an_unparseable_report_month():
    # A model returning "지난달" verbatim instead of resolving it must
    # not silently become a query for a nonexistent month.
    (_, mode, value), _ = _parse(
        {
            "target": "watchcon-a",
            "mode": "report",
            "month": "지난달",
        }
    )

    assert mode == "report"
    assert value == default_report_month()


def test_parse_health_intent_rejects_an_impossible_report_month():
    (_, mode, value), _ = _parse(
        {
            "target": "watchcon-a",
            "mode": "report",
            "month": "2026-13",
        }
    )

    assert value == default_report_month()


# --- Bedrock invocation shape --------------------------------------------


def test_parse_health_intent_sends_the_original_text_as_the_prompt():
    _, mocked = _parse(
        {"target": "watchcon-a", "mode": "live"},
        text="watchcon-a 최근 30분 상태 확인해줘",
    )

    assert (
        mocked.call_args.kwargs["user_prompt"]
        == "watchcon-a 최근 30분 상태 확인해줘"
    )


def test_parse_health_intent_forces_the_resolve_query_tool():
    _, mocked = _parse(
        {"target": "watchcon-a", "mode": "live"}
    )

    tool_spec = mocked.call_args.kwargs[
        "tool_spec"
    ]
    assert (
        tool_spec["toolSpec"]["name"]
        == "resolve_db_health_query"
    )
