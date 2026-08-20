"""Tests for the pure row-merging logic in src.ai.db_health_summary."""

from src.ai.db_health_summary import _merge_baseline


def test_merge_baseline_adds_columns_matched_by_resource_id():
    rows = [
        {"resource_id": "watchcon-a", "cpu_avg": "10.5"},
        {"resource_id": "watchcon-c", "cpu_avg": "11.0"},
    ]
    baseline_rows = [
        {
            "resource_id": "watchcon-a",
            "cpu_avg_baseline": "10.6",
        },
        {
            "resource_id": "watchcon-c",
            "cpu_avg_baseline": "11.2",
        },
    ]

    merged = _merge_baseline(rows, baseline_rows)

    assert merged[0]["cpu_avg_baseline"] == "10.6"
    assert merged[1]["cpu_avg_baseline"] == "11.2"


def test_merge_baseline_leaves_row_unchanged_when_no_match():
    rows = [{"resource_id": "watchcon-a", "cpu_avg": "10.5"}]
    baseline_rows = [
        {
            "resource_id": "some-other-resource",
            "cpu_avg_baseline": "1.0",
        }
    ]

    merged = _merge_baseline(rows, baseline_rows)

    assert "cpu_avg_baseline" not in merged[0]


def test_merge_baseline_handles_empty_baseline():
    rows = [{"resource_id": "watchcon-a", "cpu_avg": "10.5"}]

    merged = _merge_baseline(rows, [])

    assert merged == rows
