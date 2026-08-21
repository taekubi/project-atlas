"""Correlate live DB health with Performance Insights Top SQL using Bedrock.

Atlas has already computed the exact numbers from two sources: live
CloudWatch metrics (src.query.live_health) and the Top SQL load ranking
from Performance Insights (src.query.top_sql); this module only asks
the model to explain what the two together suggest -- a
correlation-based root-cause estimate and a concrete next step, not a
guess invented from nothing. True root cause still needs the DBA to
read the actual query plan, so the model is told to frame this as an
estimate to verify, not a certainty.
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
from src.collectors.rds_inventory import (
    collect_rds_inventory,
)
from src.query.live_health import (
    fetch_live_health_batch,
)
from src.query.top_sql import (
    fetch_top_sql,
    resolve_pi_dbi_resource_ids,
)

_DEFAULT_MODEL_ID = (
    "apac.anthropic.claude-3-5-sonnet-20241022-v2:0"
)

_SYSTEM_PROMPT = (
    "You are Project Atlas, an operations assistant for an AWS "
    "Cloud DBA. Atlas has already computed exact numbers from two "
    "sources for the same recent window: live CloudWatch metrics "
    "(CPU/connections/IOPS/latency) and Performance Insights' Top "
    "SQL ranking, where avg_active_sessions is Average Active "
    "Sessions attributable to each SQL statement -- PI's standard "
    "measure of database load, not a literal event count. You only "
    "interpret the numbers you are given and never invent a figure "
    "that is not present in the data. Correlate the two: if "
    "CPU/connections/IOPS look elevated and one or two SQL "
    "statements dominate avg_active_sessions, name them as the "
    "likely driver and suggest a concrete next step (e.g. a missing "
    "index, an N+1 pattern, an unexpectedly large IN clause, a lock "
    "wait). This is a correlation-based estimate, not a certainty -- "
    "say so, since confirming true root cause needs the DBA to read "
    "the actual query plan. If a resource has no Top SQL data for "
    "the window, say so instead of guessing. Answer in Korean, "
    "concise and DBA-oriented."
)


def _format_health_row(
    row: dict[str, str | None],
) -> str:
    return ", ".join(
        f"{key}={value}"
        for key, value in row.items()
        if key != "resource_id"
    )


def _format_prompt(
    health_rows: list[dict[str, str | None]],
    top_sql_by_resource: dict[
        str, list[dict[str, str | float | None]]
    ],
    lookback_minutes: int,
) -> str:
    """Render the combined CloudWatch + Top SQL data as plain text."""

    lines = [
        f"lookback_minutes={lookback_minutes}",
        "",
    ]

    for row in health_rows:
        resource_id = row["resource_id"]

        lines.append(
            f"[{resource_id}] CloudWatch: "
            f"{_format_health_row(row)}"
        )

        top_sql_rows = top_sql_by_resource.get(
            resource_id, []
        )

        if not top_sql_rows:
            lines.append(
                f"[{resource_id}] Top SQL: "
                "no Performance Insights data "
                "for this window"
            )
        else:
            for rank, sql_row in enumerate(
                top_sql_rows, start=1
            ):
                lines.append(
                    f"[{resource_id}] Top SQL #{rank}: "
                    "avg_active_sessions="
                    f"{sql_row['avg_active_sessions']}, "
                    f"sql_id={sql_row['sql_id']}, "
                    f"sql_text={sql_row['sql_text']}"
                )

        lines.append("")

    return "\n".join(lines)


def summarize_top_sql(
    target_session: boto3.Session,
    bedrock_session: boto3.Session,
    resource_ids: list[str],
    dbi_resource_id_by_resource_id: dict[
        str, str
    ],
    lookback_minutes: int,
    max_results: int = 10,
    model_id: str = _DEFAULT_MODEL_ID,
) -> tuple[
    list[dict[str, str | None]],
    dict[
        str, list[dict[str, str | float | None]]
    ],
    str,
]:
    """Fetch live health + Top SQL for each resource and return an AI estimate.

    `target_session` is reused for both CloudWatch and Performance
    Insights calls, since both run against the same target-account
    role. `dbi_resource_id_by_resource_id` should already be filtered
    to resources with Performance Insights enabled (see
    src.query.top_sql.resolve_pi_dbi_resource_ids) -- a resource_id
    with no entry there simply gets no Top SQL data in this summary.
    """

    health_rows = fetch_live_health_batch(
        session=target_session,
        resource_ids=resource_ids,
        lookback_minutes=lookback_minutes,
    )

    top_sql_by_resource: dict[
        str, list[dict[str, str | float | None]]
    ] = {}

    for resource_id in resource_ids:
        dbi_resource_id = (
            dbi_resource_id_by_resource_id.get(
                resource_id
            )
        )

        if dbi_resource_id is None:
            continue

        top_sql_by_resource[resource_id] = (
            fetch_top_sql(
                session=target_session,
                dbi_resource_id=dbi_resource_id,
                lookback_minutes=lookback_minutes,
                max_results=max_results,
            )
        )

    if not any(top_sql_by_resource.values()):
        return (
            health_rows,
            top_sql_by_resource,
            "해당 기간에 Performance Insights에 기록된 "
            "SQL 활동이 없습니다.",
        )

    user_prompt = _format_prompt(
        health_rows=health_rows,
        top_sql_by_resource=top_sql_by_resource,
        lookback_minutes=lookback_minutes,
    )

    summary = invoke_model(
        session=bedrock_session,
        model_id=model_id,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
    )

    return health_rows, top_sql_by_resource, summary


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Run the Project Atlas Top SQL "
            "analysis (CloudWatch + "
            "Performance Insights) and "
            "interpret it with Bedrock."
        )
    )

    parser.add_argument(
        "--target-profile",
        default="atlas-target",
        help=(
            "AWS CLI profile with direct access to "
            "the target account (CloudWatch + "
            "Performance Insights)"
        ),
    )
    parser.add_argument(
        "--bedrock-profile",
        default="atlas-test",
        help=(
            "AWS CLI profile for the Atlas storage "
            "account (Bedrock)"
        ),
    )
    parser.add_argument(
        "--target-region",
        default="ap-northeast-2",
        help="AWS Region running the target resource",
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
        "--resource-id",
        required=True,
        action="append",
        help=(
            "DB instance identifier to analyze "
            "(repeatable for multiple instances)"
        ),
    )
    parser.add_argument(
        "--lookback-minutes",
        type=int,
        default=10,
        help="Lookback window in minutes",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=10,
        help="Number of top SQL statements to fetch per instance",
    )

    return parser.parse_args()


def main() -> None:
    """Run the Top SQL analysis CLI."""

    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="replace",
    )

    args = parse_arguments()

    try:
        target_session = boto3.Session(
            profile_name=args.target_profile,
            region_name=args.target_region,
        )
        bedrock_session = create_bedrock_session(
            profile_name=args.bedrock_profile,
            region_name=args.bedrock_region,
        )

        inventory = collect_rds_inventory(
            session=target_session
        )

        (
            dbi_resource_id_by_resource_id,
            resource_ids_without_pi,
        ) = resolve_pi_dbi_resource_ids(
            resource_ids=args.resource_id,
            inventory=inventory,
        )

        if resource_ids_without_pi:
            print(
                "Performance Insights가 꺼져 있어 "
                "건너뜀: "
                f"{', '.join(resource_ids_without_pi)}"
            )

        if not dbi_resource_id_by_resource_id:
            raise SystemExit(
                "Performance Insights가 켜진 "
                "리소스가 없습니다."
            )

        (
            health_rows,
            top_sql_by_resource,
            summary,
        ) = summarize_top_sql(
            target_session=target_session,
            bedrock_session=bedrock_session,
            resource_ids=args.resource_id,
            dbi_resource_id_by_resource_id=(
                dbi_resource_id_by_resource_id
            ),
            lookback_minutes=(
                args.lookback_minutes
            ),
            max_results=args.max_results,
            model_id=args.model_id,
        )

    except ProfileNotFound as error:
        raise SystemExit(
            f"AWS profile not found: {error}"
        ) from error
    except ValueError as error:
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
    print("PROJECT ATLAS TOP SQL ANALYSIS")
    print("=" * 60)
    print(
        f"resource_ids={', '.join(args.resource_id)} "
        f"lookback_minutes={args.lookback_minutes}"
    )
    print()

    for row in health_rows:
        print(row)

    print()

    for resource_id, sql_rows in (
        top_sql_by_resource.items()
    ):
        print(f"[{resource_id}] Top SQL:")

        for sql_row in sql_rows:
            print(f"  {sql_row}")

    print()
    print("-" * 60)
    print(summary)


if __name__ == "__main__":
    main()
