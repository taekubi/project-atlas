"""Turn a month of aggregated metrics into a written report using Bedrock.

Atlas computes every figure and every month-over-month change (see
src/query/monthly_report.py); this module only asks the model to write
them up, per the project principle that Atlas produces trustworthy
numbers and AI interprets them rather than inventing its own.

The prompt differs from the other summaries in what it asks for. The
live and Top SQL summaries answer "is this resource healthy right now";
a monthly report is written for someone who was not watching -- so it
leads with what changed, states the figures behind each claim, and is
explicit about how much of the month the data actually covers rather
than presenting a partial month as a full one.
"""

from __future__ import annotations

import argparse
import sys
import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ProfileNotFound,
)

from src.ai.bedrock_client import (
    BedrockInvocationError,
    create_session as create_bedrock_session,
    invoke_model,
)
from src.query.athena_client import (
    AthenaQueryError,
    create_session as create_athena_session,
)
from src.query.monthly_report import (
    compute_monthly_report,
    default_report_month,
    run_monthly_report,
)

_DEFAULT_MODEL_ID = (
    "apac.anthropic.claude-3-5-sonnet-20241022-v2:0"
)

# A month of several resources produces a longer prompt and a longer
# answer than the point-in-time summaries, which fit comfortably in the
# shared 1024-token default.
_MAX_SUMMARY_TOKENS = 2048

_BYTES_PER_GB = 1024**3

_SYSTEM_PROMPT = (
    "You are Project Atlas, an operations assistant for an AWS "
    "Cloud DBA. You are writing the monthly operations report for a "
    "fleet of RDS/Aurora databases, for a reader who was not "
    "watching day to day. Atlas has already aggregated every figure "
    "from its own metric history; you only interpret the numbers you "
    "are given and never invent one that is not present. "
    "Each resource line gives this month's values, the previous "
    "month's (*_previous), and the change (*_change_pct, a percentage "
    "where positive means it went up). Lead with what changed: call "
    "out the notable month-over-month movements and always cite both "
    "figures behind a claim, not just the percentage. Treat a change "
    "under about 10% as normal variation rather than a finding. "
    "Read coverage before trusting an average: active_days out of "
    "days_in_month is how many days actually have data. "
    "coverage_note='partial_month' means the figures cover only part "
    "of the month and must be presented with that caveat; "
    "'month_in_progress' means the month has not finished yet; "
    "'no_data' means this resource has no data for the month at all "
    "(it may be new or decommissioned) -- say so rather than "
    "reporting zeros as measurements. A null change_pct means there "
    "is no comparable previous month, not a change of zero. "
    "freeable_memory_min_gb and free_storage_space_min_gb are the "
    "lowest point reached during the month; volume_bytes_used_max_gb "
    "is an Aurora cluster's used storage, which auto-scales -- growth "
    "there is normal and is not a capacity risk. "
    "Structure the answer as: a short overall paragraph, then one "
    "section per resource with its notable changes, then a brief list "
    "of anything worth acting on next month. If nothing notable "
    "changed, say so plainly instead of manufacturing findings. "
    "Answer in Korean, concise and DBA-oriented."
)


def _to_gb(
    value: float | None,
) -> float | None:
    """Convert bytes to GB for the prompt, keeping None as None."""

    if value is None:
        return None

    return round(value / _BYTES_PER_GB, 2)


def _format_change(
    entry: dict[str, str | int | float | None],
    column: str,
    label: str,
) -> str | None:
    """Render one metric as 'label=now (prev=…, change=…%)'."""

    current = entry.get(column)

    if current is None:
        return None

    parts = [f"{label}={current}"]

    previous = entry.get(
        f"{column}_previous"
    )
    change = entry.get(
        f"{column}_change_pct"
    )

    if previous is not None:
        parts.append(f"prev={previous}")

    if change is not None:
        parts.append(f"change={change}%")

    if len(parts) == 1:
        return parts[0]

    return (
        f"{parts[0]} "
        f"({', '.join(parts[1:])})"
    )


def _format_report_entry(
    entry: dict[str, str | int | float | None],
) -> str:
    """Render one resource's month as plain text for the model prompt."""

    fields = [
        f"resource_id={entry['resource_id']}",
        f"engine={entry.get('engine')}",
        f"cluster_role={entry.get('cluster_role')}",
        f"active_days={entry['active_days']}",
        f"days_in_month={entry['days_in_month']}",
        "previous_active_days="
        f"{entry['previous_active_days']}",
        f"coverage_note={entry['coverage_note']}",
    ]

    for column, label in (
        ("cpu_avg", "cpu_avg"),
        ("cpu_max", "cpu_max"),
        (
            "connections_avg",
            "connections_avg",
        ),
        (
            "connections_max",
            "connections_max",
        ),
        (
            "read_latency_avg",
            "read_latency_avg",
        ),
        (
            "write_latency_avg",
            "write_latency_avg",
        ),
        ("read_iops_avg", "read_iops_avg"),
        (
            "write_iops_avg",
            "write_iops_avg",
        ),
        (
            "disk_queue_depth_avg",
            "disk_queue_depth_avg",
        ),
    ):
        rendered = _format_change(
            entry, column, label
        )

        if rendered is not None:
            fields.append(rendered)

    for column, label in (
        (
            "aurora_replica_lag_max",
            "aurora_replica_lag_max",
        ),
    ):
        value = entry.get(column)

        if value is not None:
            fields.append(f"{label}={value}")

    for column, label in (
        (
            "freeable_memory_min_bytes",
            "freeable_memory_min_gb",
        ),
        (
            "free_storage_space_min_bytes",
            "free_storage_space_min_gb",
        ),
        (
            "volume_bytes_used_max",
            "volume_bytes_used_max_gb",
        ),
    ):
        value = _to_gb(entry.get(column))

        if value is not None:
            fields.append(f"{label}={value}")

    return ", ".join(fields)


def format_report_prompt(
    report: list[dict[str, str | int | float | None]],
    month: str,
    account_id: str,
    region: str,
) -> str:
    """Render the whole report as plain text for the model prompt."""

    lines = [
        f"report_month={month}",
        f"account_id={account_id} region={region}",
        f"resource_count={len(report)}",
        "",
    ]

    for entry in report:
        lines.append(
            _format_report_entry(entry)
        )

    return "\n".join(lines)


def summarize_monthly_report(
    athena_session: boto3.Session,
    bedrock_session: boto3.Session,
    output_location: str,
    account_id: str,
    region: str,
    month: str,
    resource_ids: list[str] | None = None,
    model_id: str = _DEFAULT_MODEL_ID,
    database: str = "project_atlas",
    table: str = "cloudwatch_metrics",
    workgroup: str = "primary",
) -> tuple[
    list[dict[str, str | int | float | None]], str
]:
    """Aggregate a month of metrics and return it with an AI-written report."""

    rows = run_monthly_report(
        session=athena_session,
        output_location=output_location,
        account_id=account_id,
        region=region,
        month=month,
        resource_ids=resource_ids,
        database=database,
        table=table,
        workgroup=workgroup,
    )

    report = compute_monthly_report(
        report_rows=rows,
        month=month,
    )

    if not report:
        return (
            [],
            f"{month}에 해당하는 데이터가 없습니다.",
        )

    summary = invoke_model(
        session=bedrock_session,
        model_id=model_id,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=format_report_prompt(
            report=report,
            month=month,
            account_id=account_id,
            region=region,
        ),
        max_tokens=_MAX_SUMMARY_TOKENS,
    )

    return report, summary


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Generate the Project Atlas "
            "monthly operations report and "
            "write it up with Bedrock."
        )
    )

    parser.add_argument(
        "--profile",
        default="atlas-test",
        help=(
            "AWS CLI profile for the Atlas "
            "storage account (Athena + Bedrock)"
        ),
    )
    parser.add_argument(
        "--athena-region",
        default="ap-northeast-2",
        help="AWS Region running Athena",
    )
    parser.add_argument(
        "--bedrock-region",
        default="ap-northeast-2",
        help="AWS Region running Bedrock",
    )
    parser.add_argument(
        "--database",
        default="project_atlas",
        help="Athena database name",
    )
    parser.add_argument(
        "--table",
        default="cloudwatch_metrics",
        help="Athena table name",
    )
    parser.add_argument(
        "--output-location",
        required=True,
        help="S3 location for Athena query results",
    )
    parser.add_argument(
        "--workgroup",
        default="primary",
        help="Athena workgroup",
    )
    parser.add_argument(
        "--account-id",
        required=True,
        help="Target account_id partition to report on",
    )
    parser.add_argument(
        "--target-region",
        required=True,
        help="Target region partition to report on",
    )
    parser.add_argument(
        "--month",
        default=None,
        help=(
            "Report month as YYYY-MM "
            "(default: the last complete month)"
        ),
    )
    parser.add_argument(
        "--resource-id",
        default=None,
        action="append",
        help=(
            "Narrow the report to one resource_id "
            "(repeatable; default: every resource)"
        ),
    )
    parser.add_argument(
        "--model-id",
        default=_DEFAULT_MODEL_ID,
        help="Bedrock model ID or inference profile ID",
    )

    return parser.parse_args()


def main() -> None:
    """Run the monthly report CLI."""

    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="replace",
    )

    args = parse_arguments()
    month = (
        args.month or default_report_month()
    )

    try:
        athena_session = create_athena_session(
            profile_name=args.profile,
            region_name=args.athena_region,
        )
        bedrock_session = create_bedrock_session(
            profile_name=args.profile,
            region_name=args.bedrock_region,
        )

        report, summary = (
            summarize_monthly_report(
                athena_session=athena_session,
                bedrock_session=bedrock_session,
                output_location=(
                    args.output_location
                ),
                account_id=args.account_id,
                region=args.target_region,
                month=month,
                resource_ids=args.resource_id,
                model_id=args.model_id,
                database=args.database,
                table=args.table,
                workgroup=args.workgroup,
            )
        )

    except ProfileNotFound as error:
        raise SystemExit(
            f"AWS profile not found: {error}"
        ) from error
    except ValueError as error:
        raise SystemExit(str(error)) from error
    except AthenaQueryError as error:
        raise SystemExit(str(error)) from error
    except BedrockInvocationError as error:
        raise SystemExit(str(error)) from error
    except (
        ClientError,
        BotoCoreError,
    ) as error:
        raise SystemExit(
            f"Atlas monthly report failed: {error}"
        ) from error

    print()
    print("=" * 60)
    print("PROJECT ATLAS MONTHLY REPORT")
    print("=" * 60)
    print(
        f"account_id={args.account_id} "
        f"region={args.target_region} "
        f"month={month}"
    )
    print()

    for entry in report:
        print(
            f"[{entry['resource_id']}] "
            f"active_days={entry['active_days']}"
            f"/{entry['days_in_month']} "
            f"cpu_avg={entry['cpu_avg']} "
            "change="
            f"{entry.get('cpu_avg_change_pct')}%"
        )

    print()
    print("-" * 60)
    print(summary)


if __name__ == "__main__":
    main()
