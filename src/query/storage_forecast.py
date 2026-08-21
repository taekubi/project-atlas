"""Forecast per-resource storage trends from Curated CloudWatch history.

The DB Health Snapshot and baseline layers each answer "how is this
resource doing" for a point in time or a trailing average. Neither
answers a DBA's other common question: "at this rate, when do we run
out of disk?" This module pulls each resource's daily storage history
over a trailing window from Curated Parquet (via Athena) and fits a
straight line through it, so Atlas can project a concrete answer
instead of leaving the AI to guess at a trend from raw numbers.

Two different metrics feed this, one per storage model:
- Standard RDS engines publish FreeStorageSpace per instance -- free
  space shrinks over time, so a declining trend projects toward an
  exhaustion date.
- Aurora's storage auto-scales and has no per-instance free-space
  metric; CloudWatch instead publishes VolumeBytesUsed once per
  cluster. Usage is expected to grow, so there is no "exhaustion" to
  project -- only a growth rate worth surfacing.
Both are queried together and disambiguated per resource by which
column is populated (see compute_storage_forecast), since a given
resource_id only ever has one or the other.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

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
_DEFAULT_LOOKBACK_DAYS = 30

# Fewer daily points than this is too noisy to trust a fitted trend --
# report "not enough history yet" instead of a shaky projection.
_MIN_HISTORY_POINTS = 3


def build_storage_history_query(
    account_id: str,
    region: str,
    start_date: str,
    end_date: str,
    resource_ids: list[str] | None = None,
    database: str = _DEFAULT_DATABASE,
    table: str = _DEFAULT_TABLE,
) -> str:
    """Build the daily storage history SQL for a trailing window.

    Returns one row per resource/day with both a FreeStorageSpace
    column (standard RDS, per instance) and a VolumeBytesUsed column
    (Aurora, per cluster) -- a given resource_id only ever populates
    one of the two, so compute_storage_forecast picks whichever is
    present.
    """

    validate_account_id(account_id)
    validate_region(region)
    validate_date(start_date)
    validate_date(end_date)

    resource_filter = ""

    if resource_ids:
        for resource_id in resource_ids:
            validate_resource_id(resource_id)

        quoted_ids = ", ".join(
            f"'{resource_id}'"
            for resource_id in resource_ids
        )
        resource_filter = (
            f"AND resource_id IN ({quoted_ids})"
        )

    return (
        "SELECT\n"
        "    resource_id,\n"
        "    date,\n"
        "    MIN(CASE WHEN metric_name = "
        "'FreeStorageSpace' THEN value END) "
        "AS free_storage_space_bytes,\n"
        "    MAX(CASE WHEN metric_name = "
        "'VolumeBytesUsed' THEN value END) "
        "AS volume_bytes_used\n"
        f"FROM {database}.{table}\n"
        f"WHERE account_id = '{account_id}'\n"
        f"  AND region = '{region}'\n"
        "  AND metric_name IN "
        "('FreeStorageSpace', 'VolumeBytesUsed')\n"
        f"  AND date BETWEEN '{start_date}' "
        f"AND '{end_date}'\n"
        f"  {resource_filter}\n"
        "GROUP BY resource_id, date\n"
        "ORDER BY resource_id, date"
    )


def run_storage_history(
    session: boto3.Session,
    output_location: str,
    account_id: str,
    region: str,
    start_date: str,
    end_date: str,
    resource_ids: list[str] | None = None,
    database: str = _DEFAULT_DATABASE,
    table: str = _DEFAULT_TABLE,
    workgroup: str = "primary",
) -> list[dict[str, str | None]]:
    """Run the storage history query and return one row per resource/day."""

    query = build_storage_history_query(
        account_id=account_id,
        region=region,
        start_date=start_date,
        end_date=end_date,
        resource_ids=resource_ids,
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


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _fit_line(
    xs: list[int],
    ys: list[float],
) -> tuple[float, float]:
    """Fit y = slope*x + intercept by ordinary least squares."""

    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    denominator = sum(
        (x - mean_x) ** 2 for x in xs
    )

    if denominator == 0:
        return 0.0, mean_y

    numerator = sum(
        (x - mean_x) * (y - mean_y)
        for x, y in zip(xs, ys)
    )

    slope = numerator / denominator
    intercept = mean_y - slope * mean_x

    return slope, intercept


def _forecast_for_resource(
    resource_id: str,
    points: list[tuple[str, float, str]],
) -> dict[str, str | int | float | None]:
    """Fit a trend line for one resource's sorted (date, value, metric) points."""

    points = sorted(points, key=lambda point: point[0])
    latest_date, latest_value, storage_metric = (
        points[-1]
    )

    base_row: dict[str, str | int | float | None] = {
        "resource_id": resource_id,
        "storage_metric": storage_metric,
        "history_days": len(points),
        "latest_date": latest_date,
        "latest_value_bytes": latest_value,
        "trend_bytes_per_day": None,
        "projected_exhaustion_date": None,
        "days_until_exhaustion": None,
        "forecast_note": None,
    }

    if len(points) < _MIN_HISTORY_POINTS:
        base_row["forecast_note"] = "insufficient_history"
        return base_row

    first_date = _parse_date(points[0][0])
    xs = [
        (_parse_date(point_date) - first_date).days
        for point_date, _, _ in points
    ]
    ys = [value for _, value, _ in points]

    slope, intercept = _fit_line(xs, ys)

    base_row["trend_bytes_per_day"] = round(slope, 2)

    # Only FreeStorageSpace (shrinking free space) has a meaningful
    # "exhaustion date" -- Aurora's VolumeBytesUsed is expected to grow
    # since its storage auto-scales, so there is no ceiling to project
    # toward.
    if (
        storage_metric == "FreeStorageSpace"
        and slope < 0
    ):
        zero_crossing_x = -intercept / slope
        days_from_latest = max(
            round(zero_crossing_x - xs[-1]), 0
        )

        base_row["days_until_exhaustion"] = days_from_latest
        base_row["projected_exhaustion_date"] = (
            _parse_date(latest_date)
            + timedelta(days=days_from_latest)
        ).isoformat()

    return base_row


def compute_storage_forecast(
    history_rows: list[dict[str, str | None]],
) -> list[dict[str, str | int | float | None]]:
    """Compute a linear storage trend forecast per resource.

    Groups the daily storage history by resource_id, fits a straight
    line (least squares) through value vs. elapsed days, and -- for a
    declining FreeStorageSpace trend -- projects the date free space
    would reach zero if it continues unchanged. This is an explicit
    linear extrapolation, not a guarantee: workload changes, storage
    autoscaling, or one-off cleanups can all break the trend, so
    callers should present it as an estimate rather than a fact.

    Each resource_id is expected to carry only one of the two storage
    columns (FreeStorageSpace for standard RDS instances,
    VolumeBytesUsed for Aurora clusters) -- see
    build_storage_history_query.
    """

    points_by_resource: dict[
        str, list[tuple[str, float, str]]
    ] = defaultdict(list)

    for row in history_rows:
        resource_id = row.get("resource_id")
        row_date = row.get("date")

        if not resource_id or not row_date:
            continue

        free_storage_value = row.get(
            "free_storage_space_bytes"
        )
        volume_used_value = row.get(
            "volume_bytes_used"
        )

        if free_storage_value is not None:
            points_by_resource[resource_id].append(
                (
                    row_date,
                    float(free_storage_value),
                    "FreeStorageSpace",
                )
            )
        elif volume_used_value is not None:
            points_by_resource[resource_id].append(
                (
                    row_date,
                    float(volume_used_value),
                    "VolumeBytesUsed",
                )
            )

    return [
        _forecast_for_resource(resource_id, points)
        for resource_id, points in points_by_resource.items()
    ]
