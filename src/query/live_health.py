"""Live DB Health Snapshot sourced directly from CloudWatch.

Unlike db_health.py (which reads day-granularity Curated Parquet via
Athena -- a batch store suited to historical/report queries), this module
calls CloudWatch GetMetricData directly for a short recent lookback
window, for monitoring questions like "how is this DB doing right now"
where the Curated batch layer's latency is not acceptable.

Each metric is requested with a single Period spanning the whole lookback
window, so CloudWatch returns at most one datapoint per metric -- the
aggregate statistic (avg/max/min) over that window, matching the shape of
db_health.py's per-resource row (same column names) so the AI
interpretation and Slack formatting layers can be reused unchanged.
"""

from __future__ import annotations

import re
from datetime import (
    datetime,
    timedelta,
    timezone,
)

import boto3

_RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

_MIN_LOOKBACK_MINUTES = 1
_MAX_LOOKBACK_MINUTES = 24 * 60

# (metric_name, statistic, output column) -- output columns match the
# DB Health Snapshot query's column names in db_health.py.
_METRIC_STATS: list[tuple[str, str, str]] = [
    ("CPUUtilization", "Average", "cpu_avg"),
    ("CPUUtilization", "Maximum", "cpu_max"),
    ("FreeableMemory", "Minimum", "freeable_memory_min_bytes"),
    ("DatabaseConnections", "Average", "connections_avg"),
    ("DatabaseConnections", "Maximum", "connections_max"),
    ("ReadLatency", "Average", "read_latency_avg"),
    ("WriteLatency", "Average", "write_latency_avg"),
    ("ReadIOPS", "Average", "read_iops_avg"),
    ("WriteIOPS", "Average", "write_iops_avg"),
    ("DiskQueueDepth", "Average", "disk_queue_depth_avg"),
    ("AuroraReplicaLag", "Maximum", "aurora_replica_lag_max"),
    ("FreeStorageSpace", "Minimum", "free_storage_space_min_bytes"),
]


def _validate_resource_id(
    resource_id: str,
) -> str:
    """Validate a resource_id used as a CloudWatch dimension value."""

    if not _RESOURCE_ID_PATTERN.match(
        resource_id
    ):
        raise ValueError(
            f"resource_id is invalid: {resource_id!r}"
        )

    return resource_id


def _validate_lookback_minutes(
    lookback_minutes: int,
) -> int:
    """Validate the lookback window length."""

    if not (
        _MIN_LOOKBACK_MINUTES
        <= lookback_minutes
        <= _MAX_LOOKBACK_MINUTES
    ):
        raise ValueError(
            "lookback_minutes must be between "
            f"{_MIN_LOOKBACK_MINUTES} and "
            f"{_MAX_LOOKBACK_MINUTES}, got "
            f"{lookback_minutes}"
        )

    return lookback_minutes


def build_metric_data_queries(
    resource_id: str,
    period_seconds: int,
) -> tuple[
    list[dict],
    dict[str, str],
]:
    """Build one GetMetricData query per (metric, statistic) pair."""

    queries: list[dict] = []
    column_by_query_id: dict[str, str] = {}

    for index, (
        metric_name,
        statistic,
        column,
    ) in enumerate(_METRIC_STATS):
        query_id = f"m{index}"
        column_by_query_id[query_id] = column

        queries.append(
            {
                "Id": query_id,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/RDS",
                        "MetricName": (
                            metric_name
                        ),
                        "Dimensions": [
                            {
                                "Name": "DBInstanceIdentifier",
                                "Value": resource_id,
                            }
                        ],
                    },
                    "Period": period_seconds,
                    "Stat": statistic,
                },
                "ReturnData": True,
            }
        )

    return queries, column_by_query_id


def fetch_live_health(
    session: boto3.Session,
    resource_id: str,
    lookback_minutes: int,
) -> dict[str, str | None]:
    """Fetch one live DB Health Snapshot row from CloudWatch."""

    _validate_resource_id(resource_id)
    _validate_lookback_minutes(
        lookback_minutes
    )

    period_seconds = lookback_minutes * 60

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(
        minutes=lookback_minutes
    )

    queries, column_by_query_id = (
        build_metric_data_queries(
            resource_id=resource_id,
            period_seconds=period_seconds,
        )
    )

    cloudwatch = session.client(
        "cloudwatch"
    )

    response = cloudwatch.get_metric_data(
        MetricDataQueries=queries,
        StartTime=start_time,
        EndTime=end_time,
        ScanBy="TimestampDescending",
    )

    row: dict[str, str | None] = {
        "resource_id": resource_id,
    }

    for result in response.get(
        "MetricDataResults",
        [],
    ):
        column = column_by_query_id[
            result["Id"]
        ]
        values = result.get(
            "Values",
            [],
        )

        row[column] = (
            str(round(float(values[0]), 4))
            if values
            else None
        )

    return row
