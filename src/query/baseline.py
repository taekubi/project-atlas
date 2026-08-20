"""Compute historical per-resource metric baselines from Curated data.

The DB Health Snapshot (db_health.py) and live query (live_health.py)
each show a single point-in-time reading. Without something to compare
that reading against, an AI asked "is this healthy" can only guess at
generic thresholds -- it has no idea what is normal for this specific
resource. This module computes each resource's own average and standard
deviation over a trailing window from Curated Parquet (via Athena), so
the AI can reason about deviation from this deployment's actual history
instead.
"""

from __future__ import annotations

import boto3

from src.query.athena_client import run_query
from src.query.validators import (
    validate_account_id,
    validate_date,
    validate_region,
    validate_resource_id,
)

_DEFAULT_DATABASE = "project_atlas"
_DEFAULT_TABLE = "cloudwatch_metrics"
_DEFAULT_LOOKBACK_DAYS = 7

# (metric_name, aggregate function, output column)
_BASELINE_STATS: list[tuple[str, str, str]] = [
    ("CPUUtilization", "AVG", "cpu_avg_baseline"),
    ("CPUUtilization", "STDDEV", "cpu_stddev_baseline"),
    ("DatabaseConnections", "AVG", "connections_avg_baseline"),
    (
        "DatabaseConnections",
        "STDDEV",
        "connections_stddev_baseline",
    ),
    ("ReadLatency", "AVG", "read_latency_avg_baseline"),
    ("WriteLatency", "AVG", "write_latency_avg_baseline"),
    ("ReadIOPS", "AVG", "read_iops_avg_baseline"),
    ("WriteIOPS", "AVG", "write_iops_avg_baseline"),
    (
        "DiskQueueDepth",
        "AVG",
        "disk_queue_depth_avg_baseline",
    ),
    (
        "AuroraReplicaLag",
        "AVG",
        "aurora_replica_lag_avg_baseline",
    ),
    (
        "AuroraReplicaLag",
        "MAX",
        "aurora_replica_lag_max_baseline",
    ),
    (
        "FreeableMemory",
        "AVG",
        "freeable_memory_avg_baseline_bytes",
    ),
    (
        "FreeStorageSpace",
        "AVG",
        "free_storage_space_avg_baseline_bytes",
    ),
]


def build_baseline_query(
    account_id: str,
    region: str,
    start_date: str,
    end_date: str,
    resource_id: str | None = None,
    database: str = _DEFAULT_DATABASE,
    table: str = _DEFAULT_TABLE,
) -> str:
    """Build the per-resource metric baseline SQL for a trailing window."""

    validate_account_id(account_id)
    validate_region(region)
    validate_date(start_date)
    validate_date(end_date)

    resource_filter = ""

    if resource_id is not None:
        validate_resource_id(resource_id)
        resource_filter = (
            f"AND resource_id = '{resource_id}'"
        )

    select_columns = ",\n    ".join(
        [
            "resource_id",
            *[
                f"ROUND({agg}(CASE WHEN metric_name = "
                f"'{metric_name}' THEN value END), 4) "
                f"AS {column}"
                for metric_name, agg, column in (
                    _BASELINE_STATS
                )
            ],
        ]
    )

    return (
        f"SELECT\n    {select_columns}\n"
        f"FROM {database}.{table}\n"
        f"WHERE account_id = '{account_id}'\n"
        f"  AND region = '{region}'\n"
        f"  AND date BETWEEN '{start_date}' "
        f"AND '{end_date}'\n"
        f"  {resource_filter}\n"
        "GROUP BY resource_id\n"
        "ORDER BY resource_id"
    )


def run_baseline(
    session: boto3.Session,
    output_location: str,
    account_id: str,
    region: str,
    start_date: str,
    end_date: str,
    resource_id: str | None = None,
    database: str = _DEFAULT_DATABASE,
    table: str = _DEFAULT_TABLE,
    workgroup: str = "primary",
) -> list[dict[str, str | None]]:
    """Run the baseline query and return one row per resource."""

    query = build_baseline_query(
        account_id=account_id,
        region=region,
        start_date=start_date,
        end_date=end_date,
        resource_id=resource_id,
        database=database,
        table=table,
    )

    return run_query(
        session=session,
        database=database,
        output_location=output_location,
        query=query,
        workgroup=workgroup,
    )
