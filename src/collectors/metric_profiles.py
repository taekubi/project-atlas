"""Resolve CloudWatch metric profiles for RDS resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PROFILE_NAME = "operational-v1"


METRIC_DEFINITIONS: dict[str, dict[str, str]] = {
    "CPUUtilization": {
        "statistic": "Average",
        "unit": "Percent",
    },
    "DatabaseConnections": {
        "statistic": "Average",
        "unit": "Count",
    },
    "FreeableMemory": {
        "statistic": "Minimum",
        "unit": "Bytes",
    },
    "ReadIOPS": {
        "statistic": "Average",
        "unit": "Count/Second",
    },
    "WriteIOPS": {
        "statistic": "Average",
        "unit": "Count/Second",
    },
    "ReadLatency": {
        "statistic": "Average",
        "unit": "Seconds",
    },
    "WriteLatency": {
        "statistic": "Average",
        "unit": "Seconds",
    },
    "ReadThroughput": {
        "statistic": "Average",
        "unit": "Bytes/Second",
    },
    "WriteThroughput": {
        "statistic": "Average",
        "unit": "Bytes/Second",
    },
    "DiskQueueDepth": {
        "statistic": "Average",
        "unit": "Count",
    },
    "NetworkReceiveThroughput": {
        "statistic": "Average",
        "unit": "Bytes/Second",
    },
    "NetworkTransmitThroughput": {
        "statistic": "Average",
        "unit": "Bytes/Second",
    },

    # Standard RDS storage
    "FreeStorageSpace": {
        "statistic": "Minimum",
        "unit": "Bytes",
    },

    # Aurora/local storage
    "FreeLocalStorage": {
        "statistic": "Minimum",
        "unit": "Bytes",
    },

    # Aurora replication
    "AuroraReplicaLag": {
        "statistic": "Maximum",
        "unit": "Milliseconds",
    },
    "AuroraReplicaLagMaximum": {
        "statistic": "Maximum",
        "unit": "Milliseconds",
    },

    # MySQL / MariaDB
    "BinLogDiskUsage": {
        "statistic": "Maximum",
        "unit": "Bytes",
    },

    # PostgreSQL
    "TransactionLogsDiskUsage": {
        "statistic": "Maximum",
        "unit": "Bytes",
    },

    # MySQL / MariaDB / PostgreSQL / Oracle
    "SwapUsage": {
        "statistic": "Maximum",
        "unit": "Bytes",
    },

    # SQL Server
    "FailedSQLServerAgentJobsCount": {
        "statistic": "Sum",
        "unit": "Count",
    },
    "TempDbAvailableDataSpace": {
        "statistic": "Minimum",
        "unit": "Bytes",
    },
    "TempDbAvailableLogSpace": {
        "statistic": "Minimum",
        "unit": "Bytes",
    },
    "TempDbDataFileUsage": {
        "statistic": "Maximum",
        "unit": "Percent",
    },
    "TempDbLogFileUsage": {
        "statistic": "Maximum",
        "unit": "Percent",
    },

    # Burstable instance / storage
    "CPUCreditBalance": {
        "statistic": "Minimum",
        "unit": "Count",
    },
    "BurstBalance": {
        "statistic": "Minimum",
        "unit": "Percent",
    },

    # Future: non-Aurora RDS read replica profile
    "ReplicaLag": {
        "statistic": "Maximum",
        "unit": "Seconds",
    },
}


BASE_INSTANCE_METRICS = (
    "CPUUtilization",
    "DatabaseConnections",
    "FreeableMemory",
    "ReadIOPS",
    "WriteIOPS",
    "ReadLatency",
    "WriteLatency",
    "ReadThroughput",
    "WriteThroughput",
    "DiskQueueDepth",
    "NetworkReceiveThroughput",
    "NetworkTransmitThroughput",
)


@dataclass(frozen=True)
class MetricProfileSelection:
    """Resolved metric profile for one discovered DB resource."""

    profile_name: str
    resource_profile: str
    metrics: tuple[str, ...]


def _append_unique(
    metrics: list[str],
    *metric_names: str,
) -> None:
    """Append metrics while preserving order and uniqueness."""

    for metric_name in metric_names:
        if metric_name not in metrics:
            metrics.append(metric_name)


def _is_aurora(engine: str) -> bool:
    """Return whether an engine is Amazon Aurora."""

    return engine.startswith("aurora")


def _is_oracle(engine: str) -> bool:
    """Return whether an engine is Oracle."""

    return engine.startswith("oracle")


def _is_sql_server(engine: str) -> bool:
    """Return whether an engine is Microsoft SQL Server."""

    return engine.startswith("sqlserver")


def _is_burstable(instance_class: str) -> bool:
    """Return whether an instance uses a burstable T class."""

    return instance_class.startswith(
        (
            "db.t2.",
            "db.t3.",
            "db.t4g.",
        )
    )


def _supports_swap_usage(engine: str) -> bool:
    """Return whether RDS publishes SwapUsage for the engine."""

    return (
        engine in {
            "mysql",
            "mariadb",
            "postgres",
        }
        or _is_oracle(engine)
    )


def resolve_metric_profile(
    resource: dict[str, Any],
    profile_name: str = PROFILE_NAME,
) -> MetricProfileSelection:
    """Resolve metrics from discovered RDS resource metadata."""

    if profile_name != PROFILE_NAME:
        raise ValueError(
            f"Unsupported metric profile: {profile_name}"
        )

    engine = str(
        resource.get("engine") or ""
    ).lower()

    cluster_role = str(
        resource.get("cluster_role") or ""
    ).lower()

    instance_class = str(
        resource.get("instance_class") or ""
    ).lower()

    storage_type = str(
        resource.get("storage_type") or ""
    ).lower()

    metrics = list(
        BASE_INSTANCE_METRICS
    )

    if _is_aurora(engine):
        resource_profile = (
            f"aurora-{cluster_role}"
            if cluster_role
            else "aurora-instance"
        )

        if instance_class != "db.serverless":
            _append_unique(
                metrics,
                "FreeLocalStorage",
            )

        if cluster_role == "writer":
            _append_unique(
                metrics,
                "AuroraReplicaLagMaximum",
            )

        elif cluster_role == "reader":
            _append_unique(
                metrics,
                "AuroraReplicaLag",
            )

    else:
        resource_profile = (
            f"rds-{engine}"
            if engine
            else "rds-generic"
        )

        _append_unique(
            metrics,
            "FreeStorageSpace",
        )

        if engine in {
            "mysql",
            "mariadb",
        }:
            _append_unique(
                metrics,
                "BinLogDiskUsage",
            )

        elif engine == "postgres":
            _append_unique(
                metrics,
                "TransactionLogsDiskUsage",
            )

        if _supports_swap_usage(engine):
            _append_unique(
                metrics,
                "SwapUsage",
            )

        if _is_sql_server(engine):
            _append_unique(
                metrics,
                "TempDbAvailableDataSpace",
                "TempDbAvailableLogSpace",
                "TempDbDataFileUsage",
                "TempDbLogFileUsage",
            )

            # SQL Server Express doesn't provide SQL Server Agent.
            if engine != "sqlserver-ex":
                _append_unique(
                    metrics,
                    "FailedSQLServerAgentJobsCount",
                )

        if storage_type == "gp2":
            _append_unique(
                metrics,
                "BurstBalance",
            )

    if _is_burstable(instance_class):
        _append_unique(
            metrics,
            "CPUCreditBalance",
        )

    return MetricProfileSelection(
        profile_name=profile_name,
        resource_profile=resource_profile,
        metrics=tuple(metrics),
    )