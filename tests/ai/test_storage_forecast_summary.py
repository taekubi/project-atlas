"""Tests for the pure prompt-formatting logic in src.ai.storage_forecast_summary."""

from src.ai.storage_forecast_summary import (
    _format_forecast_row,
)

_BYTES_PER_GB = 1024**3


def test_format_forecast_row_converts_bytes_to_gb():
    row = {
        "resource_id": "watchcon-a",
        "storage_metric": "FreeStorageSpace",
        "history_days": 30,
        "latest_date": "2026-08-21",
        "latest_value_bytes": 500 * _BYTES_PER_GB,
        "trend_bytes_per_day": -10 * _BYTES_PER_GB,
        "projected_exhaustion_date": "2026-10-06",
        "days_until_exhaustion": 46,
        "forecast_note": None,
    }

    text = _format_forecast_row(row)

    assert "storage_metric=FreeStorageSpace" in text
    assert "latest_value_gb=500.0" in text
    assert "trend_gb_per_day=-10.0" in text
    assert "projected_exhaustion_date=2026-10-06" in text
    assert "days_until_exhaustion=46" in text


def test_format_forecast_row_handles_insufficient_history():
    row = {
        "resource_id": "watchcon-a",
        "storage_metric": "FreeStorageSpace",
        "history_days": 2,
        "latest_date": "2026-08-21",
        "latest_value_bytes": 500 * _BYTES_PER_GB,
        "trend_bytes_per_day": None,
        "projected_exhaustion_date": None,
        "days_until_exhaustion": None,
        "forecast_note": "insufficient_history",
    }

    text = _format_forecast_row(row)

    assert "trend_gb_per_day=None" in text
    assert "forecast_note=insufficient_history" in text


def test_format_forecast_row_marks_aurora_volume_metric():
    row = {
        "resource_id": "watchcon-cluster-cluster",
        "storage_metric": "VolumeBytesUsed",
        "history_days": 30,
        "latest_date": "2026-08-21",
        "latest_value_bytes": 500 * _BYTES_PER_GB,
        "trend_bytes_per_day": 10 * _BYTES_PER_GB,
        "projected_exhaustion_date": None,
        "days_until_exhaustion": None,
        "forecast_note": None,
    }

    text = _format_forecast_row(row)

    assert "storage_metric=VolumeBytesUsed" in text
    assert "latest_value_gb=500.0" in text
    assert "trend_gb_per_day=10.0" in text
    assert "projected_exhaustion_date=None" in text
