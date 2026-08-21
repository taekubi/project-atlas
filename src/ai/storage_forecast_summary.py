"""Interpret a Storage Capacity Forecast into a DBA-facing summary using Bedrock.

Atlas computes the exact trend and projected exhaustion date (see
src/query/storage_forecast.py); this module only asks the model to
explain what those numbers mean, per the project principle that Atlas
produces trustworthy numbers and AI interprets them rather than
inventing its own.
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
)
from src.query.storage_forecast import (
    compute_storage_forecast,
    run_storage_history,
)

_DEFAULT_MODEL_ID = (
    "apac.anthropic.claude-3-5-sonnet-20241022-v2:0"
)

_BYTES_PER_GB = 1024**3

_SYSTEM_PROMPT = (
    "You are Project Atlas, an operations assistant for an AWS "
    "Cloud DBA. Atlas has already computed each resource's storage "
    "trend by fitting a straight line through its daily storage "
    "history and extrapolating forward; you only interpret the "
    "numbers you are given and never invent a figure that is not "
    "present in the data. This is a linear estimate, not a "
    "guarantee -- say so when a resource is trending toward "
    "exhaustion, since workload changes or cleanups can break the "
    "trend. Each row's storage_metric tells you what kind of "
    "resource it is and how to read latest_value_gb: "
    "'FreeStorageSpace' means standard RDS free space -- latest_value_gb "
    "is *free* space remaining, and a negative trend_gb_per_day is a "
    "real exhaustion risk worth flagging with the projected date and "
    "days remaining. 'VolumeBytesUsed' means an Aurora cluster -- "
    "latest_value_gb is *used* space, its storage auto-scales, and "
    "growth (positive trend_gb_per_day) is normal and NOT a risk -- "
    "report the current usage and growth rate informationally, never "
    "frame it as running out of space, and ignore "
    "projected_exhaustion_date/days_until_exhaustion for these rows "
    "even if present. If forecast_note is 'insufficient_history', say "
    "there is not yet enough history for that resource instead of "
    "guessing. Answer in Korean, concise and DBA-oriented."
)


def _format_forecast_row(
    row: dict[str, str | int | float | None],
) -> str:
    """Render one forecast row as plain text for the model prompt."""

    latest_value_bytes = row["latest_value_bytes"]
    trend_bytes_per_day = row["trend_bytes_per_day"]

    latest_value_gb = (
        round(latest_value_bytes / _BYTES_PER_GB, 2)
        if latest_value_bytes is not None
        else None
    )
    trend_gb_per_day = (
        round(trend_bytes_per_day / _BYTES_PER_GB, 4)
        if trend_bytes_per_day is not None
        else None
    )

    return ", ".join(
        [
            f"resource_id={row['resource_id']}",
            f"storage_metric={row['storage_metric']}",
            f"history_days={row['history_days']}",
            f"latest_date={row['latest_date']}",
            f"latest_value_gb={latest_value_gb}",
            f"trend_gb_per_day={trend_gb_per_day}",
            "projected_exhaustion_date="
            f"{row['projected_exhaustion_date']}",
            f"days_until_exhaustion={row['days_until_exhaustion']}",
            f"forecast_note={row['forecast_note']}",
        ]
    )


def summarize_storage_forecast(
    athena_session: boto3.Session,
    bedrock_session: boto3.Session,
    output_location: str,
    account_id: str,
    region: str,
    resource_ids: list[str] | None,
    lookback_days: int,
    model_id: str = _DEFAULT_MODEL_ID,
    database: str = "project_atlas",
    table: str = "cloudwatch_metrics",
    workgroup: str = "primary",
) -> tuple[
    list[dict[str, str | int | float | None]], str
]:
    """Fetch a storage capacity forecast and return it with an AI-written summary."""

    end_date = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")
    start_date = (
        datetime.now(timezone.utc)
        - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d")

    history_rows = run_storage_history(
        session=athena_session,
        output_location=output_location,
        account_id=account_id,
        region=region,
        start_date=start_date,
        end_date=end_date,
        resource_ids=resource_ids,
        database=database,
        table=table,
        workgroup=workgroup,
    )

    if not history_rows:
        return (
            [],
            "해당 조건에 해당하는 스토리지 데이터가 없습니다.",
        )

    forecasts = compute_storage_forecast(
        history_rows
    )

    user_prompt = "\n".join(
        [
            f"account_id={account_id} region={region} "
            f"lookback_days={lookback_days}",
            "",
            *[
                _format_forecast_row(row)
                for row in forecasts
            ],
        ]
    )

    summary = invoke_model(
        session=bedrock_session,
        model_id=model_id,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
    )

    return forecasts, summary


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Run the Project Atlas Storage "
            "Capacity Forecast and interpret "
            "it with Bedrock."
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
        "--lookback-days",
        type=int,
        default=30,
        help="Trailing window of daily history to fit the trend on",
    )
    parser.add_argument(
        "--resource-id",
        default=None,
        help=(
            "Narrow the forecast to one resource_id "
            "(default: every resource in the account/region)"
        ),
    )

    return parser.parse_args()


def main() -> None:
    """Run the Storage Capacity Forecast CLI."""

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

        forecasts, summary = (
            summarize_storage_forecast(
                athena_session=athena_session,
                bedrock_session=bedrock_session,
                output_location=(
                    args.output_location
                ),
                account_id=args.account_id,
                region=args.target_region,
                resource_ids=(
                    [args.resource_id]
                    if args.resource_id
                    else None
                ),
                lookback_days=args.lookback_days,
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
            f"Atlas AI summary failed: {error}"
        ) from error

    print()
    print("=" * 60)
    print("PROJECT ATLAS STORAGE CAPACITY FORECAST")
    print("=" * 60)
    print(
        f"account_id={args.account_id} "
        f"region={args.target_region} "
        f"lookback_days={args.lookback_days}"
    )
    print()

    for row in forecasts:
        print(row)

    print()
    print("-" * 60)
    print(summary)


if __name__ == "__main__":
    main()
