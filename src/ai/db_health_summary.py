"""Interpret a DB Health Snapshot into a DBA-facing summary using Bedrock.

Atlas computes the exact operational numbers (see src/query/db_health.py);
this module only asks the model to explain what those numbers mean, per the
project principle that Atlas produces trustworthy numbers and AI interprets
them rather than inventing its own.
"""

from __future__ import annotations

import argparse
import sys
from datetime import (
    datetime,
    timedelta,
    timezone,
)

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
    format_table,
)
from src.query.baseline import run_baseline
from src.query.db_health import run_db_health_snapshot
from src.query.live_health import fetch_live_health

_DEFAULT_MODEL_ID = (
    "apac.anthropic.claude-3-5-sonnet-20241022-v2:0"
)

_DEFAULT_BASELINE_LOOKBACK_DAYS = 7

_SYSTEM_PROMPT = (
    "You are Project Atlas, an operations assistant for an AWS "
    "Cloud DBA. Atlas has already computed exact CloudWatch "
    "metrics; you only interpret the numbers you are given, and "
    "you never invent a figure that is not present in the data. "
    "Rows may include *_baseline columns (and *_stddev_baseline "
    "for CPU/connections) -- this resource's own historical "
    "average over roughly the last 7 days. Judge whether the "
    "current values are unusual for THIS resource by comparing "
    "them against its own baseline, not generic thresholds. If "
    "baseline columns are missing for a resource, say you lack "
    "historical context for it rather than guessing a threshold. "
    "For each resource, say whether it looks healthy or needs "
    "attention and why, referencing the specific figures. If a "
    "field is empty, treat it as not applicable to that resource "
    "rather than as zero. Answer in Korean, concise and "
    "DBA-oriented."
)


def _format_rows_as_prompt(
    rows: list[dict[str, str | None]],
    account_id: str,
    region: str,
    date: str,
) -> str:
    """Render snapshot rows as plain text for the model prompt."""

    lines = [
        f"account_id={account_id} region={region} date={date}",
        "",
    ]

    for row in rows:
        lines.append(
            ", ".join(
                f"{key}={value}"
                for key, value in row.items()
            )
        )

    return "\n".join(lines)


def _merge_baseline(
    rows: list[dict[str, str | None]],
    baseline_rows: list[dict[str, str | None]],
) -> list[dict[str, str | None]]:
    """Merge baseline columns into each row, matched by resource_id."""

    baseline_by_resource = {
        row["resource_id"]: row
        for row in baseline_rows
        if row.get("resource_id")
    }

    for row in rows:
        baseline_row = baseline_by_resource.get(
            row.get("resource_id")
        )

        if baseline_row:
            row.update(
                {
                    key: value
                    for key, value in (
                        baseline_row.items()
                    )
                    if key != "resource_id"
                }
            )

    return rows


def _fetch_baseline_safe(
    athena_session: boto3.Session,
    output_location: str,
    account_id: str,
    region: str,
    start_date: str,
    end_date: str,
    resource_id: str | None,
    database: str,
    table: str,
    workgroup: str,
) -> list[dict[str, str | None]]:
    """Fetch the historical baseline, or [] if it cannot be computed.

    The baseline is an enrichment, not a requirement -- a resource with
    too little history, or a transient Athena issue, should not stop
    the current reading from being interpreted.
    """

    try:
        return run_baseline(
            session=athena_session,
            output_location=output_location,
            account_id=account_id,
            region=region,
            start_date=start_date,
            end_date=end_date,
            resource_id=resource_id,
            database=database,
            table=table,
            workgroup=workgroup,
        )
    except AthenaQueryError:
        return []


def summarize_db_health(
    athena_session: boto3.Session,
    bedrock_session: boto3.Session,
    output_location: str,
    account_id: str,
    region: str,
    date: str,
    resource_id: str | None = None,
    model_id: str = _DEFAULT_MODEL_ID,
    database: str = "project_atlas",
    table: str = "cloudwatch_metrics",
    workgroup: str = "primary",
    baseline_lookback_days: int = (
        _DEFAULT_BASELINE_LOOKBACK_DAYS
    ),
) -> tuple[list[dict[str, str | None]], str]:
    """Fetch a DB Health Snapshot and return it with an AI-written summary."""

    rows = run_db_health_snapshot(
        session=athena_session,
        output_location=output_location,
        account_id=account_id,
        region=region,
        date=date,
        resource_id=resource_id,
        database=database,
        table=table,
        workgroup=workgroup,
    )

    if not rows:
        return rows, "해당 조건에 해당하는 데이터가 없습니다."

    baseline_start_date = (
        datetime.strptime(date, "%Y-%m-%d")
        - timedelta(days=baseline_lookback_days)
    ).strftime("%Y-%m-%d")

    baseline_rows = _fetch_baseline_safe(
        athena_session=athena_session,
        output_location=output_location,
        account_id=account_id,
        region=region,
        start_date=baseline_start_date,
        end_date=date,
        resource_id=resource_id,
        database=database,
        table=table,
        workgroup=workgroup,
    )

    rows = _merge_baseline(rows, baseline_rows)

    user_prompt = _format_rows_as_prompt(
        rows=rows,
        account_id=account_id,
        region=region,
        date=date,
    )

    summary = invoke_model(
        session=bedrock_session,
        model_id=model_id,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
    )

    return rows, summary


def summarize_live_db_health(
    cloudwatch_session: boto3.Session,
    athena_session: boto3.Session,
    bedrock_session: boto3.Session,
    output_location: str,
    account_id: str,
    region: str,
    resource_id: str,
    lookback_minutes: int,
    model_id: str = _DEFAULT_MODEL_ID,
    database: str = "project_atlas",
    table: str = "cloudwatch_metrics",
    workgroup: str = "primary",
    baseline_lookback_days: int = (
        _DEFAULT_BASELINE_LOOKBACK_DAYS
    ),
) -> tuple[list[dict[str, str | None]], str]:
    """Fetch a live DB Health Snapshot and return it with an AI-written summary.

    Unlike summarize_db_health (Curated/Athena, day-granularity), the
    current reading comes from CloudWatch directly for a recent lookback
    window, since batch latency is not acceptable for a monitoring
    question. The historical baseline it is compared against, however,
    still comes from Curated/Athena (account_id/region/output_location
    are only used for that baseline lookup) -- best-effort, since a
    missing baseline should not block a live reading.
    """

    row = fetch_live_health(
        session=cloudwatch_session,
        resource_id=resource_id,
        lookback_minutes=lookback_minutes,
    )

    rows = [row]

    metric_columns = [
        key
        for key in row
        if key != "resource_id"
    ]

    if all(
        row.get(column) is None
        for column in metric_columns
    ):
        return rows, (
            f"'{resource_id}'에 대한 CloudWatch 데이터를 "
            "찾을 수 없습니다. 정확한 DB/인스턴스 식별자인지 "
            "확인해주세요."
        )

    end_date = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")
    start_date = (
        datetime.now(timezone.utc)
        - timedelta(days=baseline_lookback_days)
    ).strftime("%Y-%m-%d")

    baseline_rows = _fetch_baseline_safe(
        athena_session=athena_session,
        output_location=output_location,
        account_id=account_id,
        region=region,
        start_date=start_date,
        end_date=end_date,
        resource_id=resource_id,
        database=database,
        table=table,
        workgroup=workgroup,
    )

    rows = _merge_baseline(rows, baseline_rows)
    row = rows[0]

    label = (
        f"resource_id={resource_id} "
        f"lookback_minutes={lookback_minutes}"
    )

    user_prompt = "\n".join(
        [
            label,
            "",
            ", ".join(
                f"{key}={value}"
                for key, value in row.items()
            ),
        ]
    )

    summary = invoke_model(
        session=bedrock_session,
        model_id=model_id,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
    )

    return rows, summary


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Run the Project Atlas DB Health "
            "Snapshot and interpret it with "
            "Bedrock."
        )
    )

    parser.add_argument(
        "--profile",
        default="atlas-test",
        help="AWS CLI profile",
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
        "--model-id",
        default=_DEFAULT_MODEL_ID,
        help="Bedrock model ID or inference profile ID",
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
        help="Target account_id partition to query",
    )
    parser.add_argument(
        "--target-region",
        required=True,
        help="Target region partition to query",
    )
    parser.add_argument(
        "--date",
        required=True,
        help="Target date partition to query (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--resource-id",
        default=None,
        help=(
            "Narrow the snapshot to one resource_id "
            "(default: every resource in the account/region)"
        ),
    )

    return parser.parse_args()


def main() -> None:
    """Run the DB Health Summary CLI."""

    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="replace",
    )

    args = parse_arguments()

    try:
        athena_session = create_athena_session(
            profile_name=args.profile,
            region_name=args.athena_region,
        )
        bedrock_session = create_bedrock_session(
            profile_name=args.profile,
            region_name=args.bedrock_region,
        )

        rows, summary = summarize_db_health(
            athena_session=athena_session,
            bedrock_session=bedrock_session,
            output_location=args.output_location,
            account_id=args.account_id,
            region=args.target_region,
            date=args.date,
            resource_id=args.resource_id,
            model_id=args.model_id,
            database=args.database,
            table=args.table,
            workgroup=args.workgroup,
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
            f"Atlas AI summary failed: {error}"
        ) from error

    print()
    print("=" * 60)
    print("PROJECT ATLAS DB HEALTH SUMMARY")
    print("=" * 60)
    print(
        f"account_id={args.account_id} "
        f"region={args.target_region} "
        f"date={args.date}"
    )
    print()
    print(format_table(rows))
    print()
    print("-" * 60)
    print(summary)


if __name__ == "__main__":
    main()
