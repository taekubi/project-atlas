"""Tests for the pure prompt-formatting logic in src.ai.top_sql_summary."""

from src.ai.top_sql_summary import _format_prompt


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
