"""Tests for src.query.baseline query building (no AWS calls)."""

import pytest

from src.query.baseline import build_baseline_query


def test_build_baseline_query_covers_trailing_window():
    query = build_baseline_query(
        account_id="826846563965",
        region="ap-northeast-2",
        start_date="2026-08-13",
        end_date="2026-08-19",
    )

    assert (
        "date BETWEEN '2026-08-13' AND '2026-08-19'" in query
    )
    assert "cpu_avg_baseline" in query
    assert "cpu_stddev_baseline" in query


def test_build_baseline_query_filters_multiple_resources():
    query = build_baseline_query(
        account_id="826846563965",
        region="ap-northeast-2",
        start_date="2026-08-13",
        end_date="2026-08-19",
        resource_ids=["watchcon-a", "watchcon-c"],
    )

    assert (
        "resource_id IN ('watchcon-a', 'watchcon-c')" in query
    )


def test_build_baseline_query_rejects_invalid_date():
    with pytest.raises(ValueError):
        build_baseline_query(
            account_id="826846563965",
            region="ap-northeast-2",
            start_date="not-a-date",
            end_date="2026-08-19",
        )
