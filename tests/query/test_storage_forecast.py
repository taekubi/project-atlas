"""Tests for src.query.storage_forecast (no AWS calls)."""

import pytest

from src.query.storage_forecast import (
    build_storage_history_query,
    compute_storage_forecast,
)

_BYTES_PER_GB = 1024**3


def test_build_storage_history_query_covers_trailing_window():
    query = build_storage_history_query(
        account_id="826846563965",
        region="ap-northeast-2",
        start_date="2026-07-22",
        end_date="2026-08-21",
    )

    assert (
        "date BETWEEN '2026-07-22' AND '2026-08-21'" in query
    )
    assert "'FreeStorageSpace'" in query
    assert "'VolumeBytesUsed'" in query
    assert "GROUP BY resource_id, date" in query


def test_build_storage_history_query_filters_resources():
    query = build_storage_history_query(
        account_id="826846563965",
        region="ap-northeast-2",
        start_date="2026-07-22",
        end_date="2026-08-21",
        resource_ids=["watchcon-a"],
    )

    assert "resource_id IN ('watchcon-a')" in query


def test_build_storage_history_query_rejects_invalid_date():
    with pytest.raises(ValueError):
        build_storage_history_query(
            account_id="826846563965",
            region="ap-northeast-2",
            start_date="not-a-date",
            end_date="2026-08-21",
        )


def _history(resource_id, daily_free_gb):
    """Build consecutive daily FreeStorageSpace rows from 2026-08-01."""

    return [
        {
            "resource_id": resource_id,
            "date": f"2026-08-{day:02d}",
            "free_storage_space_bytes": str(
                round(free_gb * _BYTES_PER_GB)
            ),
            "volume_bytes_used": None,
        }
        for day, free_gb in enumerate(
            daily_free_gb, start=1
        )
    ]


def _aurora_history(resource_id, daily_used_gb):
    """Build consecutive daily VolumeBytesUsed rows from 2026-08-01."""

    return [
        {
            "resource_id": resource_id,
            "date": f"2026-08-{day:02d}",
            "free_storage_space_bytes": None,
            "volume_bytes_used": str(
                round(used_gb * _BYTES_PER_GB)
            ),
        }
        for day, used_gb in enumerate(
            daily_used_gb, start=1
        )
    ]


def test_compute_storage_forecast_projects_declining_trend():
    # Free space drops by exactly 10 GB/day starting at 500 GB.
    history = _history(
        "watchcon-a",
        [500, 490, 480, 470, 460],
    )

    forecasts = compute_storage_forecast(history)

    assert len(forecasts) == 1
    forecast = forecasts[0]

    assert forecast["resource_id"] == "watchcon-a"
    assert forecast["storage_metric"] == "FreeStorageSpace"
    assert forecast["forecast_note"] is None
    assert forecast["trend_bytes_per_day"] < 0
    assert forecast["days_until_exhaustion"] == 46
    assert (
        forecast["projected_exhaustion_date"]
        == "2026-09-20"
    )


def test_compute_storage_forecast_reports_no_risk_for_stable_trend():
    history = _history(
        "watchcon-a",
        [500, 500, 501, 500, 500],
    )

    forecasts = compute_storage_forecast(history)
    forecast = forecasts[0]

    assert forecast["trend_bytes_per_day"] >= 0
    assert forecast["projected_exhaustion_date"] is None
    assert forecast["days_until_exhaustion"] is None


def test_compute_storage_forecast_flags_insufficient_history():
    history = _history("watchcon-a", [500, 490])

    forecasts = compute_storage_forecast(history)
    forecast = forecasts[0]

    assert (
        forecast["forecast_note"]
        == "insufficient_history"
    )
    assert forecast["trend_bytes_per_day"] is None


def test_compute_storage_forecast_groups_multiple_resources():
    history = _history(
        "watchcon-a", [500, 490, 480]
    ) + _history("watchcon-c", [200, 199, 198])

    forecasts = compute_storage_forecast(history)

    assert sorted(
        f["resource_id"] for f in forecasts
    ) == ["watchcon-a", "watchcon-c"]


def test_compute_storage_forecast_skips_rows_missing_both_values():
    history = [
        {
            "resource_id": "watchcon-a",
            "date": "2026-08-01",
            "free_storage_space_bytes": None,
            "volume_bytes_used": None,
        }
    ]

    assert compute_storage_forecast(history) == []


def test_compute_storage_forecast_reports_aurora_growth_without_exhaustion():
    # Aurora cluster storage grows by 10 GB/day -- normal, auto-scaling.
    history = _aurora_history(
        "watchcon-cluster-cluster",
        [500, 510, 520, 530, 540],
    )

    forecasts = compute_storage_forecast(history)

    assert len(forecasts) == 1
    forecast = forecasts[0]

    assert (
        forecast["resource_id"]
        == "watchcon-cluster-cluster"
    )
    assert forecast["storage_metric"] == "VolumeBytesUsed"
    assert forecast["trend_bytes_per_day"] > 0
    # No exhaustion framing for Aurora, even though it's growing.
    assert forecast["projected_exhaustion_date"] is None
    assert forecast["days_until_exhaustion"] is None


def test_compute_storage_forecast_never_projects_exhaustion_for_declining_aurora_usage():
    # Even if Aurora usage were to shrink, it should not be framed as
    # an exhaustion risk -- that framing only applies to FreeStorageSpace.
    history = _aurora_history(
        "watchcon-cluster-cluster",
        [500, 490, 480, 470, 460],
    )

    forecasts = compute_storage_forecast(history)
    forecast = forecasts[0]

    assert forecast["trend_bytes_per_day"] < 0
    assert forecast["projected_exhaustion_date"] is None
    assert forecast["days_until_exhaustion"] is None
