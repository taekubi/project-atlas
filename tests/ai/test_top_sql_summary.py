"""Tests for the pure prompt-formatting logic in src.ai.top_sql_summary."""

from src.ai.top_sql_summary import (
    _format_prompt,
    _normalize_analysis,
)


def test_format_prompt_includes_cloudwatch_and_top_sql_rows():
    health_rows = [
        {
            "resource_id": "watchcon-a",
            "cpu_avg": "92.5",
            "connections_avg": "310",
        }
    ]
    top_sql_by_resource = {
        "watchcon-a": [
            {
                "sql_id": "abc123",
                "sql_text": "SELECT * FROM orders WHERE status = ?",
                "avg_active_sessions": 4.2,
            }
        ]
    }

    prompt = _format_prompt(
        health_rows, top_sql_by_resource, 10
    )

    assert "lookback_minutes=10" in prompt
    assert "[watchcon-a] CloudWatch:" in prompt
    assert "cpu_avg=92.5" in prompt
    assert "[watchcon-a] Top SQL #1:" in prompt
    assert "avg_active_sessions=4.2" in prompt
    assert "sql_id=abc123" in prompt
    assert "SELECT * FROM orders" in prompt


def test_format_prompt_marks_resources_with_no_top_sql_data():
    health_rows = [
        {"resource_id": "watchcon-c", "cpu_avg": "5.0"}
    ]

    prompt = _format_prompt(health_rows, {}, 10)

    assert (
        "[watchcon-c] Top SQL: no Performance Insights data"
        in prompt
    )


def test_format_prompt_reports_the_total_load_for_context():
    # The model is told to judge whether the load level warrants
    # attention at all, which needs the total rather than only the
    # per-statement ranking.
    top_sql_by_resource = {
        "watchcon-a": [
            {
                "sql_id": "a",
                "sql_text": "select 1",
                "avg_active_sessions": 0.2,
            },
            {
                "sql_id": "b",
                "sql_text": "select 2",
                "avg_active_sessions": 0.1,
            },
        ]
    }

    prompt = _format_prompt(
        [{"resource_id": "watchcon-a"}],
        top_sql_by_resource,
        10,
    )

    assert (
        "total avg_active_sessions=0.3" in prompt
    )
    assert "across 2 ranked statements" in prompt


def test_format_prompt_marks_a_truncated_statement():
    top_sql_by_resource = {
        "watchcon-a": [
            {
                "sql_id": "a",
                "sql_text": "SELECT …",
                "sql_text_truncated": True,
                "avg_active_sessions": 1.0,
            }
        ]
    }

    prompt = _format_prompt(
        [{"resource_id": "watchcon-a"}],
        top_sql_by_resource,
        10,
    )

    assert "sql_text_truncated=True" in prompt


def test_format_prompt_defaults_the_truncation_flag_to_false():
    top_sql_by_resource = {
        "watchcon-a": [
            {
                "sql_id": "a",
                "sql_text": "select 1",
                "avg_active_sessions": 1.0,
            }
        ]
    }

    prompt = _format_prompt(
        [{"resource_id": "watchcon-a"}],
        top_sql_by_resource,
        10,
    )

    assert "sql_text_truncated=False" in prompt


# --- _normalize_analysis ------------------------------------------------


def test_normalize_analysis_keeps_a_well_formed_entry():
    result = _normalize_analysis(
        {
            "overall": "  부하가 낮습니다.  ",
            "queries": [
                {
                    "resource_id": "watchcon-a",
                    "rank": 1,
                    "finding": "WHERE 절이 없습니다.",
                    "suggestion": "인덱스를 확인하세요.",
                    "confidence": "high",
                }
            ],
        }
    )

    assert result["overall"] == "부하가 낮습니다."
    assert result["queries"] == [
        {
            "resource_id": "watchcon-a",
            "rank": 1,
            "finding": "WHERE 절이 없습니다.",
            "suggestion": "인덱스를 확인하세요.",
            "confidence": "high",
        }
    ]


def test_normalize_analysis_coerces_a_string_rank():
    result = _normalize_analysis(
        {
            "overall": "x",
            "queries": [
                {
                    "resource_id": "watchcon-a",
                    "rank": "2",
                    "finding": "f",
                    "confidence": "low",
                }
            ],
        }
    )

    assert result["queries"][0]["rank"] == 2


def test_normalize_analysis_drops_an_entry_without_a_usable_rank():
    # An annotation that cannot be tied to a specific statement must be
    # dropped rather than rendered against the wrong query.
    result = _normalize_analysis(
        {
            "overall": "x",
            "queries": [
                {
                    "resource_id": "watchcon-a",
                    "rank": "first",
                    "finding": "f",
                    "confidence": "low",
                }
            ],
        }
    )

    assert result["queries"] == []


def test_normalize_analysis_drops_an_entry_without_a_resource_id():
    result = _normalize_analysis(
        {
            "overall": "x",
            "queries": [
                {
                    "rank": 1,
                    "finding": "f",
                    "confidence": "low",
                }
            ],
        }
    )

    assert result["queries"] == []


def test_normalize_analysis_drops_an_entry_without_a_finding():
    result = _normalize_analysis(
        {
            "overall": "x",
            "queries": [
                {
                    "resource_id": "watchcon-a",
                    "rank": 1,
                    "finding": "   ",
                    "confidence": "low",
                }
            ],
        }
    )

    assert result["queries"] == []


def test_normalize_analysis_defaults_a_missing_confidence_to_low():
    result = _normalize_analysis(
        {
            "overall": "x",
            "queries": [
                {
                    "resource_id": "watchcon-a",
                    "rank": 1,
                    "finding": "f",
                }
            ],
        }
    )

    assert (
        result["queries"][0]["confidence"]
        == "low"
    )


def test_normalize_analysis_tolerates_a_missing_queries_key():
    result = _normalize_analysis(
        {"overall": "x"}
    )

    assert result == {
        "overall": "x",
        "queries": [],
    }


def test_normalize_analysis_tolerates_a_non_dict_entry():
    result = _normalize_analysis(
        {"overall": "x", "queries": ["nope"]}
    )

    assert result["queries"] == []
