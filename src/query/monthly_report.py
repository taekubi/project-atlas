"""Aggregate a month of Curated CloudWatch history into a report.

The existing query layers each answer a question about *now*: the live
snapshot, the 7-day baseline, the storage trend. A monthly report is a
different shape of question -- "how did this month go, and what changed
since last month?" -- and it is the one a DBA has to answer to other
people rather than to themselves.

The comparison is the point. A month of averages on its own says little;
the same averages next to the previous month's turn into "connections
are up 40% on watchcon-a" -- which is the sentence that actually belongs
in a report. So both months are fetched in one Athena scan (the Curated
table is partitioned by date, and two adjacent months are a cheap
contiguous range) and paired per resource here.

Coverage is reported alongside every figure. Curated data can be thin
for a month if collection was down, and an average over 3 days must not
be presented as a month's worth -- see `active_days` and `coverage_note`.
"""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import (
    date,
    datetime,
    timedelta,
    timezone,
)

from src.query.athena_client import run_query
from src.query.validators import (
    validate_account_id,
    validate_month,
    validate_region,
    validate_resource_id,
)

_DEFAULT_DATABASE = "project_atlas"
_DEFAULT_TABLE = "cloudwatch_metrics"

# "Last month" is a calendar question, and the people asking it are in
# Korea while Lambda's clock is UTC. Resolving it in UTC would give the
# wrong month for the first nine hours of every month -- at 2026-08-01
# 03:00 KST it is still 2026-07-31 in UTC, so "last complete month"
# would answer June instead of July. Report months are therefore
# resolved in KST regardless of where the code runs.
_REPORT_TIMEZONE = timezone(
    timedelta(hours=9)
)

# Below this share of the month's days, the aggregates describe a
# handful of days rather than the month, and are flagged as such.
_PARTIAL_COVERAGE_RATIO = 0.5

# (metric_name, SQL aggregate, output column). Mirrors the DB Health
# Snapshot's column names (src.query.db_health) so a reader comparing a
# monthly figure against a daily one is looking at the same statistic.
_REPORT_STATS: list[tuple[str, str, str]] = [
    ("CPUUtilization", "AVG", "cpu_avg"),
    ("CPUUtilization", "MAX", "cpu_max"),
    ("DatabaseConnections", "AVG", "connections_avg"),
    ("DatabaseConnections", "MAX", "connections_max"),
    ("ReadLatency", "AVG", "read_latency_avg"),
    ("WriteLatency", "AVG", "write_latency_avg"),
    ("ReadIOPS", "AVG", "read_iops_avg"),
    ("WriteIOPS", "AVG", "write_iops_avg"),
    (
        "DiskQueueDepth",
        "AVG",
        "disk_queue_depth_avg",
    ),
    (
        "AuroraReplicaLag",
        "MAX",
        "aurora_replica_lag_max",
    ),
    (
        "FreeableMemory",
        "MIN",
        "freeable_memory_min_bytes",
    ),
    (
        "FreeStorageSpace",
        "MIN",
        "free_storage_space_min_bytes",
    ),
    (
        "VolumeBytesUsed",
        "MAX",
        "volume_bytes_used_max",
    ),
]

# Columns compared month over month. Storage and memory are deliberately
# excluded: a month-over-month percentage on free space is less useful
# than the storage forecast's own trend line, which already models it.
_COMPARED_COLUMNS: tuple[str, ...] = (
    "cpu_avg",
    "cpu_max",
    "connections_avg",
    "connections_max",
    "read_latency_avg",
    "write_latency_avg",
    "read_iops_avg",
    "write_iops_avg",
    "disk_queue_depth_avg",
)


def previous_month(
    month: str,
) -> str:
    """Return the calendar month before `month` (both YYYY-MM)."""

    validate_month(month)

    year, month_number = (
        int(part) for part in month.split("-")
    )

    if month_number == 1:
        return f"{year - 1}-12"

    return f"{year}-{month_number - 1:02d}"


def month_bounds(
    month: str,
) -> tuple[str, str]:
    """Return the first and last date of `month` as YYYY-MM-DD."""

    validate_month(month)

    year, month_number = (
        int(part) for part in month.split("-")
    )
    last_day = monthrange(
        year, month_number
    )[1]

    return (
        f"{month}-01",
        f"{month}-{last_day:02d}",
    )


def report_today() -> date:
    """Return today's date in the operators' timezone (KST)."""

    return datetime.now(
        _REPORT_TIMEZONE
    ).date()


def default_report_month(
    today: date | None = None,
) -> str:
    """Return the most recent complete month as YYYY-MM.

    A report defaults to the last finished month rather than the
    current one: a report covering a month still in progress compares a
    partial month against a full one, which makes every month-over-month
    figure look like a collapse.
    """

    today = today or report_today()

    return (
        today.replace(day=1)
        - timedelta(days=1)
    ).strftime("%Y-%m")


def month_day_count(
    month: str,
) -> int:
    """Return how many days `month` has."""

    validate_month(month)

    year, month_number = (
        int(part) for part in month.split("-")
    )

    return monthrange(year, month_number)[1]


def build_monthly_report_query(
    account_id: str,
    region: str,
    month: str,
    resource_ids: list[str] | None = None,
    database: str = _DEFAULT_DATABASE,
    table: str = _DEFAULT_TABLE,
) -> str:
    """Build the SQL aggregating `month` and the month before it.

    Both months come back from one scan, one row per resource per
    month, so compute_monthly_report can pair them without a second
    query. The date partition is filtered to the two months' outer
    bounds, keeping the scan to the partitions actually needed.
    """

    validate_account_id(account_id)
    validate_region(region)
    validate_month(month)

    prior = previous_month(month)
    range_start, _ = month_bounds(prior)
    _, range_end = month_bounds(month)

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

    metric_columns = ",\n    ".join(
        f"ROUND({aggregate}(CASE WHEN metric_name = "
        f"'{metric_name}' THEN value END), 4) "
        f"AS {column}"
        for metric_name, aggregate, column in (
            _REPORT_STATS
        )
    )

    return (
        "SELECT\n"
        "    resource_id,\n"
        "    SUBSTR(date, 1, 7) AS month,\n"
        "    engine,\n"
        "    cluster_role,\n"
        "    COUNT(DISTINCT date) AS active_days,\n"
        f"    {metric_columns}\n"
        f"FROM {database}.{table}\n"
        f"WHERE account_id = '{account_id}'\n"
        f"  AND region = '{region}'\n"
        f"  AND date BETWEEN '{range_start}' "
        f"AND '{range_end}'\n"
        f"  {resource_filter}\n"
        "GROUP BY resource_id, "
        "SUBSTR(date, 1, 7), engine, cluster_role\n"
        "ORDER BY resource_id, month"
    )


def run_monthly_report(
    session,
    output_location: str,
    account_id: str,
    region: str,
    month: str,
    resource_ids: list[str] | None = None,
    database: str = _DEFAULT_DATABASE,
    table: str = _DEFAULT_TABLE,
    workgroup: str = "primary",
) -> list[dict[str, str | None]]:
    """Run the monthly aggregate query, returning one row per resource/month."""

    query = build_monthly_report_query(
        account_id=account_id,
        region=region,
        month=month,
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


def _to_float(
    value: str | None,
) -> float | None:
    """Parse an Athena numeric string, treating blanks as absent."""

    if value is None or value == "":
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(
    value: str | None,
) -> int:
    """Parse an Athena integer string, treating blanks as zero."""

    parsed = _to_float(value)

    return int(parsed) if parsed is not None else 0


def _change_percent(
    current: float | None,
    prior: float | None,
) -> float | None:
    """Return the percentage change from `prior` to `current`.

    Returns None when there is nothing meaningful to express: a missing
    month, or a prior value of zero, where any increase is an infinite
    percentage and the raw numbers say more than a ratio would.
    """

    if current is None or prior is None:
        return None

    if prior == 0:
        return None

    return round(
        (current - prior) / abs(prior) * 100,
        1,
    )


def _coverage_note(
    active_days: int,
    days_in_month: int,
    is_current_month: bool,
) -> str | None:
    """Describe how much of the month the data actually covers."""

    if active_days == 0:
        return "no_data"

    if is_current_month:
        # A month still in progress cannot have full coverage, and
        # saying so would be noise rather than a warning.
        return "month_in_progress"

    if (
        active_days
        < days_in_month * _PARTIAL_COVERAGE_RATIO
    ):
        return "partial_month"

    return None


def compute_monthly_report(
    report_rows: list[dict[str, str | None]],
    month: str,
    today: date | None = None,
) -> list[dict[str, str | int | float | None]]:
    """Pair each resource's month with the month before it.

    Every resource seen in either month gets a row, so a database that
    was decommissioned (present last month, absent this one) or newly
    provisioned (the reverse) still appears rather than silently
    dropping out of the report.
    """

    validate_month(month)

    prior = previous_month(month)
    days_in_month = month_day_count(month)

    today = today or report_today()
    is_current_month = (
        today.strftime("%Y-%m") == month
    )

    by_resource: dict[
        str, dict[str, dict[str, str | None]]
    ] = defaultdict(dict)

    for row in report_rows:
        resource_id = row.get("resource_id")
        row_month = row.get("month")

        if not resource_id or row_month not in (
            month,
            prior,
        ):
            continue

        by_resource[resource_id][row_month] = row

    report: list[
        dict[str, str | int | float | None]
    ] = []

    for resource_id in sorted(by_resource):
        months = by_resource[resource_id]
        current_row = months.get(month, {})
        prior_row = months.get(prior, {})

        active_days = _to_int(
            current_row.get("active_days")
        )

        entry: dict[
            str, str | int | float | None
        ] = {
            "resource_id": resource_id,
            "month": month,
            "previous_month": prior,
            "engine": current_row.get("engine")
            or prior_row.get("engine"),
            "cluster_role": current_row.get(
                "cluster_role"
            )
            or prior_row.get("cluster_role"),
            "active_days": active_days,
            "days_in_month": days_in_month,
            "previous_active_days": _to_int(
                prior_row.get("active_days")
            ),
            "coverage_note": _coverage_note(
                active_days=active_days,
                days_in_month=days_in_month,
                is_current_month=is_current_month,
            ),
        }

        for _, _, column in _REPORT_STATS:
            current_value = _to_float(
                current_row.get(column)
            )
            entry[column] = current_value

            if column not in _COMPARED_COLUMNS:
                continue

            prior_value = _to_float(
                prior_row.get(column)
            )

            entry[f"{column}_previous"] = (
                prior_value
            )
            entry[f"{column}_change_pct"] = (
                _change_percent(
                    current_value,
                    prior_value,
                )
            )

        report.append(entry)

    return report
