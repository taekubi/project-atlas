"""Tests for src.collectors.cloudwatch_metrics (no real AWS calls)."""

from datetime import datetime, timezone

import pytest

from src.collectors.cloudwatch_metrics import (
    collect_metrics,
    save_json,
)


class _FakeCloudWatch:
    """Returns one canned response per call, optionally paginated."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def get_metric_data(self, **kwargs):
        self.requests.append(kwargs)
        return self._responses.pop(0)


def _session_with(cloudwatch):
    class _Session:
        def client(self, name):
            assert name == "cloudwatch"
            return cloudwatch

    return _Session()


def _ts(minute: int) -> datetime:
    return datetime(
        2026, 8, 21, 6, minute, 0,
        tzinfo=timezone.utc,
    )


# --- input validation ------------------------------------------------


def test_collect_metrics_rejects_an_unsupported_dimension():
    session = _session_with(
        _FakeCloudWatch([])
    )

    with pytest.raises(
        ValueError, match="dimension"
    ):
        collect_metrics(
            session=session,
            region_name="ap-northeast-2",
            resource_dimension="NotARealDimension",
            resource_id="watchcon-a",
            metric_names=["CPUUtilization"],
        )


def test_collect_metrics_rejects_an_empty_metric_list():
    session = _session_with(
        _FakeCloudWatch([])
    )

    with pytest.raises(ValueError):
        collect_metrics(
            session=session,
            region_name="ap-northeast-2",
            resource_dimension="DBInstanceIdentifier",
            resource_id="watchcon-a",
            metric_names=[],
        )


def test_collect_metrics_rejects_an_unknown_metric_name():
    session = _session_with(
        _FakeCloudWatch([])
    )

    with pytest.raises(
        ValueError, match="NotAMetric"
    ):
        collect_metrics(
            session=session,
            region_name="ap-northeast-2",
            resource_dimension="DBInstanceIdentifier",
            resource_id="watchcon-a",
            metric_names=["NotAMetric"],
        )


# --- request shape -----------------------------------------------------


def test_collect_metrics_builds_one_query_per_metric_with_correct_dimension():
    cloudwatch = _FakeCloudWatch(
        [{"MetricDataResults": []}]
    )
    session = _session_with(cloudwatch)

    collect_metrics(
        session=session,
        region_name="ap-northeast-2",
        resource_dimension="DBClusterIdentifier",
        resource_id="watchcon-cluster",
        metric_names=[
            "CPUUtilization",
            "DatabaseConnections",
        ],
        period_seconds=300,
    )

    queries = cloudwatch.requests[0][
        "MetricDataQueries"
    ]
    assert len(queries) == 2
    assert queries[0]["MetricStat"]["Metric"][
        "Dimensions"
    ] == [
        {
            "Name": "DBClusterIdentifier",
            "Value": "watchcon-cluster",
        }
    ]
    assert (
        queries[0]["MetricStat"]["Period"]
        == 300
    )


def test_collect_metrics_uses_each_metrics_own_statistic():
    cloudwatch = _FakeCloudWatch(
        [{"MetricDataResults": []}]
    )
    session = _session_with(cloudwatch)

    collect_metrics(
        session=session,
        region_name="ap-northeast-2",
        resource_dimension="DBInstanceIdentifier",
        resource_id="watchcon-a",
        metric_names=["FreeableMemory"],
    )

    query = cloudwatch.requests[0][
        "MetricDataQueries"
    ][0]
    assert (
        query["MetricStat"]["Stat"]
        == "Minimum"
    )


# --- response handling ---------------------------------------------------


def test_collect_metrics_maps_timestamps_and_values_back_to_their_metric():
    cloudwatch = _FakeCloudWatch(
        [
            {
                "MetricDataResults": [
                    {
                        "Id": "m0",
                        "Timestamps": [
                            _ts(55)
                        ],
                        "Values": [42.5],
                    },
                    {
                        "Id": "m1",
                        "Timestamps": [
                            _ts(55)
                        ],
                        "Values": [12.0],
                    },
                ]
            }
        ]
    )
    session = _session_with(cloudwatch)

    payloads = collect_metrics(
        session=session,
        region_name="ap-northeast-2",
        resource_dimension="DBInstanceIdentifier",
        resource_id="watchcon-a",
        metric_names=[
            "CPUUtilization",
            "DatabaseConnections",
        ],
    )

    by_metric = {
        p["metric_name"]: p for p in payloads
    }
    assert (
        by_metric["CPUUtilization"][
            "datapoints"
        ][0]["value"]
        == 42.5
    )
    assert (
        by_metric["DatabaseConnections"][
            "datapoints"
        ][0]["value"]
        == 12.0
    )


def test_collect_metrics_formats_timestamps_as_zulu_iso():
    cloudwatch = _FakeCloudWatch(
        [
            {
                "MetricDataResults": [
                    {
                        "Id": "m0",
                        "Timestamps": [
                            _ts(55)
                        ],
                        "Values": [1.0],
                    }
                ]
            }
        ]
    )
    session = _session_with(cloudwatch)

    payloads = collect_metrics(
        session=session,
        region_name="ap-northeast-2",
        resource_dimension="DBInstanceIdentifier",
        resource_id="watchcon-a",
        metric_names=["CPUUtilization"],
    )

    assert payloads[0]["datapoints"][0][
        "timestamp"
    ] == "2026-08-21T06:55:00Z"


def test_collect_metrics_sorts_datapoints_chronologically():
    cloudwatch = _FakeCloudWatch(
        [
            {
                "MetricDataResults": [
                    {
                        "Id": "m0",
                        "Timestamps": [
                            _ts(55),
                            _ts(45),
                        ],
                        "Values": [2.0, 1.0],
                    }
                ]
            }
        ]
    )
    session = _session_with(cloudwatch)

    payloads = collect_metrics(
        session=session,
        region_name="ap-northeast-2",
        resource_dimension="DBInstanceIdentifier",
        resource_id="watchcon-a",
        metric_names=["CPUUtilization"],
    )

    values = [
        d["value"]
        for d in payloads[0]["datapoints"]
    ]
    assert values == [1.0, 2.0]


def test_collect_metrics_follows_a_next_token_across_pages():
    cloudwatch = _FakeCloudWatch(
        [
            {
                "MetricDataResults": [
                    {
                        "Id": "m0",
                        "Timestamps": [
                            _ts(45)
                        ],
                        "Values": [1.0],
                    }
                ],
                "NextToken": "page-2",
            },
            {
                "MetricDataResults": [
                    {
                        "Id": "m0",
                        "Timestamps": [
                            _ts(55)
                        ],
                        "Values": [2.0],
                    }
                ]
            },
        ]
    )
    session = _session_with(cloudwatch)

    payloads = collect_metrics(
        session=session,
        region_name="ap-northeast-2",
        resource_dimension="DBInstanceIdentifier",
        resource_id="watchcon-a",
        metric_names=["CPUUtilization"],
    )

    assert len(cloudwatch.requests) == 2
    assert (
        cloudwatch.requests[1]["NextToken"]
        == "page-2"
    )
    assert (
        payloads[0]["datapoint_count"] == 2
    )


def test_collect_metrics_returns_an_empty_datapoint_list_for_no_data():
    cloudwatch = _FakeCloudWatch(
        [
            {
                "MetricDataResults": [
                    {"Id": "m0", "Timestamps": [], "Values": []}
                ]
            }
        ]
    )
    session = _session_with(cloudwatch)

    payloads = collect_metrics(
        session=session,
        region_name="ap-northeast-2",
        resource_dimension="DBInstanceIdentifier",
        resource_id="watchcon-a",
        metric_names=["CPUUtilization"],
    )

    assert payloads[0]["datapoints"] == []
    assert payloads[0]["datapoint_count"] == 0


def test_collect_metrics_payload_carries_the_resource_and_window_metadata():
    cloudwatch = _FakeCloudWatch(
        [{"MetricDataResults": []}]
    )
    session = _session_with(cloudwatch)

    payloads = collect_metrics(
        session=session,
        region_name="ap-northeast-2",
        resource_dimension="DBInstanceIdentifier",
        resource_id="watchcon-a",
        metric_names=["CPUUtilization"],
        lookback_minutes=30,
        period_seconds=300,
    )

    payload = payloads[0]
    assert payload["resource_id"] == "watchcon-a"
    assert (
        payload["resource_type"]
        == "DBInstance"
    )
    assert payload["region"] == "ap-northeast-2"
    assert payload["unit"] == "Percent"
    assert payload["period_seconds"] == 300


# --- save_json --------------------------------------------------------


def test_save_json_builds_a_hive_partitioned_path(tmp_path):
    payload = {
        "region": "ap-northeast-2",
        "metric_name": "CPUUtilization",
        "resource_id": "watchcon-a",
        "end_time": "2026-08-21T06:55:00Z",
    }

    output_path = save_json(
        payload, output_root=tmp_path
    )

    relative = str(
        output_path.relative_to(tmp_path)
    ).replace("\\", "/")
    assert relative == (
        "region=ap-northeast-2/"
        "metric=CPUUtilization/"
        "date=2026-08-21/"
        "watchcon-a_20260821T065500Z.json"
    )


def test_save_json_writes_readable_json(tmp_path):
    payload = {
        "region": "ap-northeast-2",
        "metric_name": "CPUUtilization",
        "resource_id": "watchcon-a",
        "end_time": "2026-08-21T06:55:00Z",
        "datapoints": [
            {
                "timestamp": "2026-08-21T06:55:00Z",
                "value": 42.5,
            }
        ],
    }

    output_path = save_json(
        payload, output_root=tmp_path
    )

    import json

    saved = json.loads(
        output_path.read_text(encoding="utf-8")
    )
    assert saved == payload
