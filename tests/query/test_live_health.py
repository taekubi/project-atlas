"""Tests for src.query.live_health query building (no AWS calls)."""

import pytest

from src.query.live_health import (
    _validate_lookback_minutes,
    build_metric_data_queries,
)


def test_build_metric_data_queries_covers_every_metric():
    queries, column_by_query_id = build_metric_data_queries(
        resource_id="watchcon-a",
        period_seconds=1800,
    )

    assert len(queries) == 12
    assert len(column_by_query_id) == 12
    assert set(column_by_query_id.values()) == {
        "cpu_avg",
        "cpu_max",
        "freeable_memory_min_bytes",
        "connections_avg",
        "connections_max",
        "read_latency_avg",
        "write_latency_avg",
        "read_iops_avg",
        "write_iops_avg",
        "disk_queue_depth_avg",
        "aurora_replica_lag_max",
        "free_storage_space_min_bytes",
    }


def test_build_metric_data_queries_uses_resource_id_dimension():
    queries, _ = build_metric_data_queries(
        resource_id="watchcon-a",
        period_seconds=1800,
    )

    for query in queries:
        dimensions = query["MetricStat"]["Metric"][
            "Dimensions"
        ]
        assert dimensions == [
            {
                "Name": "DBInstanceIdentifier",
                "Value": "watchcon-a",
            }
        ]
        assert query["MetricStat"]["Period"] == 1800


def test_validate_lookback_minutes_accepts_in_range():
    assert _validate_lookback_minutes(30) == 30
    assert _validate_lookback_minutes(1) == 1
    assert _validate_lookback_minutes(1440) == 1440


@pytest.mark.parametrize("value", [0, -5, 1441, 10000])
def test_validate_lookback_minutes_rejects_out_of_range(value):
    with pytest.raises(ValueError):
        _validate_lookback_minutes(value)
