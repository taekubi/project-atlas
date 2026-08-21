"""Tests for src.query.monthly_report (no AWS calls)."""

from datetime import date

import pytest

from src.query.monthly_report import (
    build_monthly_report_query,
    compute_monthly_report,
    default_report_month,
    month_bounds,
    month_day_count,
    previous_month,
)


def _row(
    resource_id: str,
    month: str,
    **metrics,
) -> dict:
    row = {
        "resource_id": resource_id,
        "month": month,
        "engine": "aurora-postgresql",
        "cluster_role": "writer",
        "active_days": "30",
    }
    row.update(
        {
            key: str(value)
            for key, value in metrics.items()
        }
    )
    return row


# --- calendar helpers -------------------------------------------------


def test_previous_month_steps_back_within_a_year():
    assert previous_month("2026-08") == "2026-07"


def test_previous_month_wraps_across_new_year():
    assert previous_month("2026-01") == "2025-12"


def test_month_bounds_covers_the_whole_month():
    assert month_bounds("2026-07") == (
        "2026-07-01",
        "2026-07-31",
    )


def test_month_bounds_handles_a_leap_february():
    assert month_bounds("2028-02") == (
        "2028-02-01",
        "2028-02-29",
    )


def test_month_day_count_matches_the_calendar():
    assert month_day_count("2026-02") == 28
    assert month_day_count("2026-04") == 30


def test_default_report_month_is_the_last_complete_month():
    assert (
        default_report_month(date(2026, 8, 21))
        == "2026-07"
    )


def test_default_report_month_wraps_across_new_year():
    assert (
        default_report_month(date(2026, 1, 4))
        == "2025-12"
    )


def test_invalid_month_is_rejected():
    # A malformed month would otherwise build a query that silently
    # matches no partitions.
    for bad in (
        "2026-13",
        "2026-00",
        "2026-7",
        "202608",
    ):
        with pytest.raises(ValueError):
            previous_month(bad)


# --- query building ---------------------------------------------------


def test_query_scans_both_months_in_one_range():
    query = build_monthly_report_query(
        account_id="826846563965",
        region="ap-northeast-2",
        month="2026-08",
    )

    assert (
        "date BETWEEN '2026-07-01' AND '2026-08-31'"
        in query
    )
    assert (
        "SUBSTR(date, 1, 7) AS month" in query
    )
    assert (
        "COUNT(DISTINCT date) AS active_days"
        in query
    )


def test_query_range_wraps_across_new_year():
    query = build_monthly_report_query(
        account_id="826846563965",
        region="ap-northeast-2",
        month="2026-01",
    )

    assert (
        "date BETWEEN '2025-12-01' AND '2026-01-31'"
        in query
    )


def test_query_filters_resources_when_given():
    query = build_monthly_report_query(
        account_id="826846563965",
        region="ap-northeast-2",
        month="2026-08",
        resource_ids=[
            "watchcon-a",
            "watchcon-c",
        ],
    )

    assert (
        "AND resource_id IN "
        "('watchcon-a', 'watchcon-c')" in query
    )


def test_query_rejects_an_injected_resource_id():
    with pytest.raises(ValueError):
        build_monthly_report_query(
            account_id="826846563965",
            region="ap-northeast-2",
            month="2026-08",
            resource_ids=["a'; DROP TABLE x--"],
        )


# --- report computation -----------------------------------------------


def test_compute_pairs_the_month_with_the_one_before():
    rows = [
        _row(
            "watchcon-a",
            "2026-08",
            cpu_avg=44.0,
        ),
        _row(
            "watchcon-a",
            "2026-07",
            cpu_avg=40.0,
        ),
    ]

    entry = compute_monthly_report(
        rows,
        "2026-08",
        today=date(2026, 9, 2),
    )[0]

    assert entry["cpu_avg"] == 44.0
    assert entry["cpu_avg_previous"] == 40.0
    assert entry["cpu_avg_change_pct"] == 10.0
    assert entry["previous_month"] == "2026-07"


def test_compute_reports_a_decrease_as_negative():
    rows = [
        _row(
            "watchcon-a",
            "2026-08",
            connections_avg=60.0,
        ),
        _row(
            "watchcon-a",
            "2026-07",
            connections_avg=120.0,
        ),
    ]

    entry = compute_monthly_report(
        rows,
        "2026-08",
        today=date(2026, 9, 2),
    )[0]

    assert (
        entry["connections_avg_change_pct"]
        == -50.0
    )


def test_compute_leaves_change_none_without_a_previous_month():
    # A newly provisioned database has nothing to compare against;
    # reporting 0% would read as "unchanged", which is wrong.
    rows = [
        _row(
            "new-db",
            "2026-08",
            cpu_avg=30.0,
        )
    ]

    entry = compute_monthly_report(
        rows,
        "2026-08",
        today=date(2026, 9, 2),
    )[0]

    assert entry["cpu_avg"] == 30.0
    assert entry["cpu_avg_previous"] is None
    assert entry["cpu_avg_change_pct"] is None


def test_compute_keeps_a_resource_that_disappeared():
    # Present last month, gone this month -- it must still appear, or a
    # decommissioned database silently vanishes from the report.
    rows = [
        _row(
            "retired-db",
            "2026-07",
            cpu_avg=25.0,
        )
    ]

    entry = compute_monthly_report(
        rows,
        "2026-08",
        today=date(2026, 9, 2),
    )[0]

    assert entry["resource_id"] == "retired-db"
    assert entry["active_days"] == 0
    assert entry["coverage_note"] == "no_data"
    assert entry["cpu_avg"] is None


def test_compute_avoids_dividing_by_a_zero_baseline():
    rows = [
        _row(
            "idle-db",
            "2026-08",
            connections_avg=5.0,
        ),
        _row(
            "idle-db",
            "2026-07",
            connections_avg=0.0,
        ),
    ]

    entry = compute_monthly_report(
        rows,
        "2026-08",
        today=date(2026, 9, 2),
    )[0]

    assert (
        entry["connections_avg_change_pct"]
        is None
    )
    assert entry["connections_avg"] == 5.0


def test_compute_flags_a_thinly_covered_month():
    rows = [
        {
            **_row(
                "watchcon-a",
                "2026-08",
                cpu_avg=40.0,
            ),
            "active_days": "6",
        }
    ]

    entry = compute_monthly_report(
        rows,
        "2026-08",
        today=date(2026, 9, 2),
    )[0]

    assert entry["active_days"] == 6
    assert entry["days_in_month"] == 31
    assert (
        entry["coverage_note"] == "partial_month"
    )


def test_compute_does_not_flag_a_month_still_running():
    # Low coverage is expected mid-month and should not read as a
    # collection failure.
    rows = [
        {
            **_row(
                "watchcon-a",
                "2026-08",
                cpu_avg=40.0,
            ),
            "active_days": "5",
        }
    ]

    entry = compute_monthly_report(
        rows,
        "2026-08",
        today=date(2026, 8, 5),
    )[0]

    assert (
        entry["coverage_note"]
        == "month_in_progress"
    )


def test_compute_accepts_a_fully_covered_month_without_a_note():
    rows = [
        _row(
            "watchcon-a",
            "2026-08",
            cpu_avg=40.0,
        )
    ]

    entry = compute_monthly_report(
        rows,
        "2026-08",
        today=date(2026, 9, 2),
    )[0]

    assert entry["coverage_note"] is None


def test_compute_ignores_months_outside_the_pair():
    rows = [
        _row("watchcon-a", "2026-08", cpu_avg=40.0),
        _row("watchcon-a", "2026-06", cpu_avg=99.0),
    ]

    entry = compute_monthly_report(
        rows,
        "2026-08",
        today=date(2026, 9, 2),
    )[0]

    assert entry["cpu_avg_previous"] is None


def test_compute_sorts_resources_for_a_stable_report():
    rows = [
        _row("z-db", "2026-08", cpu_avg=1.0),
        _row("a-db", "2026-08", cpu_avg=2.0),
        _row("m-db", "2026-08", cpu_avg=3.0),
    ]

    report = compute_monthly_report(
        rows,
        "2026-08",
        today=date(2026, 9, 2),
    )

    assert [
        entry["resource_id"]
        for entry in report
    ] == ["a-db", "m-db", "z-db"]


def test_compute_does_not_compare_storage_columns():
    # Month-over-month percentage on free space says less than the
    # storage forecast's own fitted trend, so it is deliberately absent.
    rows = [
        _row(
            "watchcon-a",
            "2026-08",
            free_storage_space_min_bytes=1000.0,
        )
    ]

    entry = compute_monthly_report(
        rows,
        "2026-08",
        today=date(2026, 9, 2),
    )[0]

    assert (
        entry["free_storage_space_min_bytes"]
        == 1000.0
    )
    assert (
        "free_storage_space_min_bytes_change_pct"
        not in entry
    )
