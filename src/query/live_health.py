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

from datetime import (
    datetime,
    timedelta,
    timezone,
)

import boto3

from src.query.validators import (
    validate_resource_id,
)

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


_MAX_QUERIES_PER_CALL = 500


def build_batch_metric_data_queries(
    resource_ids: list[str],
    period_seconds: int,
) -> tuple[
    list[dict],
    dict[str, tuple[str, str]],
]:
    """Build one GetMetricData query per (resource, metric, statistic).

    Reuses build_metric_data_queries per resource and renamespaces each
    query's Id with the resource's position in the list, so every Id
    stays unique when all resources' queries are sent in the same
    GetMetricData call. The returned mapping is keyed by that
    namespaced Id and points back to (resource_id, column), so a single
    batched response can be split back out per resource.
    """

    queries: list[dict] = []
    target_by_query_id: dict[
        str, tuple[str, str]
    ] = {}

    for r_index, resource_id in enumerate(
        resource_ids
    ):
        (
            resource_queries,
            column_by_query_id,
        ) = build_metric_data_queries(
            resource_id=resource_id,
            period_seconds=period_seconds,
        )

        for query in resource_queries:
            namespaced_id = (
                f"r{r_index}_{query['Id']}"
            )
            target_by_query_id[
                namespaced_id
            ] = (
                resource_id,
                column_by_query_id[
                    query["Id"]
                ],
            )
            query["Id"] = namespaced_id
            queries.append(query)

    return queries, target_by_query_id


def fetch_live_health_batch(
    session: boto3.Session,
    resource_ids: list[str],
    lookback_minutes: int,
) -> list[dict[str, str | None]]:
    """Fetch a live DB Health Snapshot row for every resource, batched.

    A separate GetMetricData call per resource means an account-wide
    query's latency grows linearly with fleet size and can reach this
    Lambda's timeout on a larger account. GetMetricData accepts up to
    500 metric queries per call, and each resource needs
    len(_METRIC_STATS) of them, so resource_ids is chunked to fit and
    each chunk costs one round trip covering every resource in it
    rather than one call per resource.

    This does not reduce the number of metrics billed -- GetMetricData
    is priced per metric requested, not per call, so the same resources
    cost the same either way. The saving is wall-clock latency (fewer
    sequential round trips) and fewer individual calls that can be
    throttled, not CloudWatch API charges.

    Returns one row per resource_id, in the same order, each shaped
    like fetch_live_health's single row.
    """

    if not resource_ids:
        return []

    for resource_id in resource_ids:
        validate_resource_id(resource_id)

    _validate_lookback_minutes(
        lookback_minutes
    )

    period_seconds = lookback_minutes * 60

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(
        minutes=lookback_minutes
    )

    cloudwatch = session.client(
        "cloudwatch"
    )

    rows_by_resource: dict[
        str, dict[str, str | None]
    ] = {
        resource_id: {
            "resource_id": resource_id
        }
        for resource_id in resource_ids
    }

    chunk_size = max(
        1,
        _MAX_QUERIES_PER_CALL
        // len(_METRIC_STATS),
    )

    for chunk_start in range(
        0, len(resource_ids), chunk_size
    ):
        chunk = resource_ids[
            chunk_start : chunk_start
            + chunk_size
        ]

        (
            queries,
            target_by_query_id,
        ) = build_batch_metric_data_queries(
            resource_ids=chunk,
            period_seconds=period_seconds,
        )

        response = (
            cloudwatch.get_metric_data(
                MetricDataQueries=queries,
                StartTime=start_time,
                EndTime=end_time,
                ScanBy="TimestampDescending",
            )
        )

        for result in response.get(
            "MetricDataResults", []
        ):
            target = target_by_query_id.get(
                result["Id"]
            )

            if target is None:
                continue

            resource_id, column = target
            values = result.get(
                "Values", []
            )

            rows_by_resource[resource_id][
                column
            ] = (
                str(round(float(values[0]), 4))
                if values
                else None
            )

    return [
        rows_by_resource[resource_id]
        for resource_id in resource_ids
    ]


def fetch_live_health(
    session: boto3.Session,
    resource_id: str,
    lookback_minutes: int,
) -> dict[str, str | None]:
    """Fetch one live DB Health Snapshot row from CloudWatch."""

    return fetch_live_health_batch(
        session=session,
        resource_ids=[resource_id],
        lookback_minutes=lookback_minutes,
    )[0]
