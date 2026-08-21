"""Fetch the top SQL statements by database load from Performance Insights.

CloudWatch can say a resource's CPU or connections are elevated, but not
which query is causing it -- that needs Amazon Performance Insights (PI),
which is only enabled per DB instance (not per cluster, and not on every
target). PI's standard way to answer "what's generating the most load
right now" is db.load.avg grouped by SQL statement: Average Active
Sessions (AAS) attributable to each statement over the window. AWS has
no separate raw "event count" metric to answer that question with, so
AAS is what this module reports and callers should present it as such.
"""

from __future__ import annotations

from datetime import (
    datetime,
    timedelta,
    timezone,
)
from typing import Any

import boto3

from src.query.validators import (
    validate_dbi_resource_id,
)

_TOP_SQL_METRIC = "db.load.avg"
_DEFAULT_MAX_RESULTS = 10
_MIN_LOOKBACK_MINUTES = 1
_MAX_LOOKBACK_MINUTES = 24 * 60
_SQL_TEXT_CHAR_LIMIT = 500


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


def _truncate_sql_text(
    sql_text: str | None,
) -> str | None:
    """Cap an SQL statement's length so one query can't blow out the prompt."""

    if sql_text is None:
        return None

    if len(sql_text) <= _SQL_TEXT_CHAR_LIMIT:
        return sql_text

    return sql_text[:_SQL_TEXT_CHAR_LIMIT] + "..."


def fetch_top_sql(
    session: boto3.Session,
    dbi_resource_id: str,
    lookback_minutes: int,
    max_results: int = _DEFAULT_MAX_RESULTS,
) -> list[dict[str, str | float | None]]:
    """Fetch the top SQL statements by Average Active Sessions for one instance."""

    validate_dbi_resource_id(dbi_resource_id)
    _validate_lookback_minutes(lookback_minutes)

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(
        minutes=lookback_minutes
    )

    pi = session.client("pi")

    response = pi.describe_dimension_keys(
        ServiceType="RDS",
        Identifier=dbi_resource_id,
        StartTime=start_time,
        EndTime=end_time,
        Metric=_TOP_SQL_METRIC,
        GroupBy={
            "Group": "db.sql",
            "Dimensions": [
                "db.sql.tokenized_id",
                "db.sql.statement",
            ],
        },
        MaxResults=max_results,
    )

    rows: list[
        dict[str, str | float | None]
    ] = []

    for key in response.get("Keys", []):
        dimensions = key.get("Dimensions", {})

        rows.append(
            {
                "sql_id": dimensions.get(
                    "db.sql.tokenized_id"
                ),
                "sql_text": _truncate_sql_text(
                    dimensions.get(
                        "db.sql.statement"
                    )
                ),
                "avg_active_sessions": round(
                    float(key.get("Total", 0.0)),
                    4,
                ),
            }
        )

    return rows


def resolve_pi_dbi_resource_ids(
    resource_ids: list[str],
    inventory: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, str], list[str]]:
    """Split resolved resource_ids by whether Performance Insights is on.

    Returns (dbi_resource_id_by_resource_id, resource_ids_without_pi).
    Performance Insights must be explicitly enabled per DB instance --
    unlike CloudWatch metrics, there is nothing to fetch for an instance
    that doesn't have it, so callers should tell the user which
    resources were skipped rather than silently dropping them.
    """

    instances_by_id = {
        instance.get("identifier"): instance
        for instance in inventory.get(
            "instances", []
        )
    }

    dbi_resource_id_by_resource_id: dict[
        str, str
    ] = {}
    resource_ids_without_pi: list[str] = []

    for resource_id in resource_ids:
        instance = instances_by_id.get(
            resource_id
        )

        dbi_resource_id = (
            instance.get("dbi_resource_id")
            if instance
            else None
        )

        if (
            instance
            and instance.get(
                "performance_insights_enabled"
            )
            and dbi_resource_id
        ):
            dbi_resource_id_by_resource_id[
                resource_id
            ] = dbi_resource_id
        else:
            resource_ids_without_pi.append(
                resource_id
            )

    return (
        dbi_resource_id_by_resource_id,
        resource_ids_without_pi,
    )
