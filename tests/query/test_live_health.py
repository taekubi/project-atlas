"""Tests for src.query.live_health query building (no AWS calls)."""

from unittest.mock import MagicMock

import pytest

from src.query.live_health import (
    _METRIC_STATS,
    _validate_lookback_minutes,
    build_batch_metric_data_queries,
    build_metric_data_queries,
    fetch_live_health,
    fetch_live_health_batch,
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


def _metric_result(query_id, value=None):
    return {
        "Id": query_id,
        "Values": [] if value is None else [value],
    }


def _session_returning(metric_data_results):
    """Build a boto3.Session double whose cloudwatch client returns
    one MetricDataResults payload per successive get_metric_data call."""

    client = MagicMock()
    client.get_metric_data.side_effect = [
        {"MetricDataResults": results}
        for results in metric_data_results
    ]

    session = MagicMock()
    session.client.return_value = client

    return session, client


def test_build_batch_metric_data_queries_covers_every_resource():
    queries, target_by_query_id = (
        build_batch_metric_data_queries(
            resource_ids=[
                "watchcon-a",
                "watchcon-c",
            ],
            period_seconds=1800,
        )
    )

    assert len(queries) == 2 * len(
        _METRIC_STATS
    )
    assert len(target_by_query_id) == len(
        queries
    )

    resources_seen = {
        target[0]
        for target in target_by_query_id.values()
    }
    assert resources_seen == {
        "watchcon-a",
        "watchcon-c",
    }


def test_build_batch_metric_data_queries_ids_stay_unique_across_resources():
    queries, _ = (
        build_batch_metric_data_queries(
            resource_ids=[
                "watchcon-a",
                "watchcon-c",
            ],
            period_seconds=1800,
        )
    )

    ids = [query["Id"] for query in queries]
    assert len(ids) == len(set(ids))


def test_build_batch_metric_data_queries_dimensions_match_their_own_resource():
    queries, target_by_query_id = (
        build_batch_metric_data_queries(
            resource_ids=[
                "watchcon-a",
                "watchcon-c",
            ],
            period_seconds=1800,
        )
    )

    for query in queries:
        resource_id, _ = target_by_query_id[
            query["Id"]
        ]
        dimensions = query["MetricStat"][
            "Metric"
        ]["Dimensions"]
        assert dimensions == [
            {
                "Name": "DBInstanceIdentifier",
                "Value": resource_id,
            }
        ]


def test_fetch_live_health_batch_splits_one_response_by_resource():
    queries, target_by_query_id = (
        build_batch_metric_data_queries(
            resource_ids=[
                "watchcon-a",
                "watchcon-c",
            ],
            period_seconds=1800,
        )
    )

    results = []
    for query_id, (
        resource_id,
        column,
    ) in target_by_query_id.items():
        value = (
            91.5
            if resource_id == "watchcon-a"
            and column == "cpu_avg"
            else None
        )
        results.append(
            _metric_result(query_id, value)
        )

    session, client = _session_returning(
        [results]
    )

    rows = fetch_live_health_batch(
        session=session,
        resource_ids=[
            "watchcon-a",
            "watchcon-c",
        ],
        lookback_minutes=30,
    )

    assert [
        row["resource_id"] for row in rows
    ] == ["watchcon-a", "watchcon-c"]
    assert rows[0]["cpu_avg"] == "91.5"
    assert rows[1]["cpu_avg"] is None
    # One account-wide fetch, not one call per resource.
    assert client.get_metric_data.call_count == 1


def test_fetch_live_health_batch_chunks_when_over_the_query_limit(
    monkeypatch,
):
    import src.query.live_health as live_health

    # 2 metrics/resource fit per chunk instead of the real 500-query
    # limit, so 3 resources forces a second GetMetricData call without
    # needing hundreds of fake resources to prove chunking happens.
    monkeypatch.setattr(
        live_health,
        "_MAX_QUERIES_PER_CALL",
        2 * len(_METRIC_STATS),
    )

    resource_ids = [
        "watchcon-a",
        "watchcon-c",
        "rds-devops",
    ]

    session, client = _session_returning(
        [[], []]
    )

    rows = fetch_live_health_batch(
        session=session,
        resource_ids=resource_ids,
        lookback_minutes=30,
    )

    assert [
        row["resource_id"] for row in rows
    ] == resource_ids
    assert client.get_metric_data.call_count == 2

    first_call_ids = {
        q["Id"]
        for q in client.get_metric_data.call_args_list[
            0
        ].kwargs["MetricDataQueries"]
    }
    second_call_ids = {
        q["Id"]
        for q in client.get_metric_data.call_args_list[
            1
        ].kwargs["MetricDataQueries"]
    }
    assert len(first_call_ids) == 2 * len(
        _METRIC_STATS
    )
    assert len(second_call_ids) == 1 * len(
        _METRIC_STATS
    )


def test_fetch_live_health_batch_returns_empty_without_calling_cloudwatch():
    session = MagicMock()

    assert (
        fetch_live_health_batch(
            session=session,
            resource_ids=[],
            lookback_minutes=30,
        )
        == []
    )
    session.client.assert_not_called()


def test_fetch_live_health_batch_rejects_an_invalid_resource_id():
    session = MagicMock()

    with pytest.raises(ValueError):
        fetch_live_health_batch(
            session=session,
            resource_ids=["bad id!"],
            lookback_minutes=30,
        )


def test_fetch_live_health_delegates_to_the_batch_fetch():
    queries, target_by_query_id = (
        build_batch_metric_data_queries(
            resource_ids=["watchcon-a"],
            period_seconds=1800,
        )
    )

    cpu_avg_query_id = next(
        query_id
        for query_id, (
            _,
            column,
        ) in target_by_query_id.items()
        if column == "cpu_avg"
    )

    session, client = _session_returning(
        [
            [
                _metric_result(
                    cpu_avg_query_id, 77.0
                )
            ]
        ]
    )

    row = fetch_live_health(
        session=session,
        resource_id="watchcon-a",
        lookback_minutes=30,
    )

    assert row["resource_id"] == "watchcon-a"
    assert row["cpu_avg"] == "77.0"
    assert client.get_metric_data.call_count == 1
