"""Correlate live DB health with Performance Insights Top SQL using Bedrock.

Atlas has already computed the exact numbers from two sources: live
CloudWatch metrics (src.query.live_health) and the Top SQL load ranking
from Performance Insights (src.query.top_sql); this module only asks
the model to explain what the two together suggest -- a
correlation-based root-cause estimate and a concrete next step, not a
guess invented from nothing. True root cause still needs the DBA to
read the actual query plan, so the model is told to frame this as an
estimate to verify, not a certainty.

The model answers through a forced tool call rather than as prose, so
each ranked statement gets its own finding/suggestion that can be
rendered directly beneath that statement in Slack. Prose covering ten
queries at once reads as a wall of text and forces the reader to match
"the third query" back to a list themselves.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ProfileNotFound,
)

from src.ai.bedrock_client import (
    BedrockInvocationError,
    create_session as create_bedrock_session,
    invoke_tool,
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

# Ten ranked statements, each with a finding and a suggestion, plus the
# overall assessment, runs past the shared 1024-token default.
_MAX_ANALYSIS_TOKENS = 3000

_TOOL_NAME = "report_top_sql_analysis"

_TOOL_SPEC = {
    "toolSpec": {
        "name": _TOOL_NAME,
        "description": (
            "Report the Top SQL analysis: one overall assessment of "
            "the current load level, plus one entry per ranked SQL "
            "statement."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "overall": {
                        "type": "string",
                        "description": (
                            "2-4 sentences in Korean. State whether "
                            "the current load level actually warrants "
                            "tuning attention, citing the figures. "
                            "Correlate the CloudWatch metrics with the "
                            "SQL ranking: if CPU/connections/IOPS look "
                            "elevated and one or two statements "
                            "dominate avg_active_sessions, say so. If "
                            "total load is low, say plainly that "
                            "nothing here is urgent rather than "
                            "manufacturing a concern."
                        ),
                    },
                    "queries": {
                        "type": "array",
                        "description": (
                            "One entry per ranked statement you have "
                            "something useful to say about. Skip a "
                            "statement rather than padding it with a "
                            "generic remark."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "resource_id": {
                                    "type": "string",
                                    "description": (
                                        "The resource_id the "
                                        "statement was ranked under, "
                                        "copied exactly."
                                    ),
                                },
                                "rank": {
                                    "type": "integer",
                                    "description": (
                                        "The statement's rank within "
                                        "that resource, as given."
                                    ),
                                },
                                "finding": {
                                    "type": "string",
                                    "description": (
                                        "One short Korean sentence: "
                                        "what about this statement is "
                                        "worth noting (e.g. no WHERE "
                                        "clause, SELECT *, a pattern "
                                        "suggesting N+1, a large IN "
                                        "list)."
                                    ),
                                },
                                "suggestion": {
                                    "type": "string",
                                    "description": (
                                        "One short Korean sentence: "
                                        "the concrete next step to "
                                        "check or try. Omit if there "
                                        "is nothing actionable."
                                    ),
                                },
                                "confidence": {
                                    "type": "string",
                                    "enum": [
                                        "high",
                                        "medium",
                                        "low",
                                    ],
                                    "description": (
                                        "How much the visible "
                                        "statement text supports the "
                                        "finding. Use 'low' whenever "
                                        "sql_text_truncated is true "
                                        "or the statement is long "
                                        "enough that its tail may be "
                                        "missing -- the cut-off part "
                                        "is exactly where a WHERE or "
                                        "LIMIT would be."
                                    ),
                                },
                            },
                            "required": [
                                "resource_id",
                                "rank",
                                "finding",
                                "confidence",
                            ],
                        },
                    },
                },
                "required": [
                    "overall",
                    "queries",
                ],
            }
        },
    }
}

_SYSTEM_PROMPT = (
    "You are Project Atlas, an operations assistant for an AWS "
    "Cloud DBA. Atlas has already computed exact numbers from two "
    "sources for the same recent window: live CloudWatch metrics "
    "(CPU/connections/IOPS/latency) and Performance Insights' Top "
    "SQL ranking, where avg_active_sessions (AAS) is Average Active "
    "Sessions attributable to each SQL statement -- PI's standard "
    "measure of database load, not a literal event count. You only "
    "interpret the numbers you are given and never invent a figure "
    "that is not present in the data. "
    "Read the load level before reading the ranking. AAS is roughly "
    "the average number of sessions actively working at once, so a "
    "total well under 1.0 means the database was close to idle for "
    "the window -- the top-ranked statement is then only the busiest "
    "of a quiet period, not a problem. Say that plainly when it is "
    "true; ranking something first does not make it worth tuning. "
    "Load becomes genuinely interesting as AAS approaches and "
    "exceeds the instance's vCPU count. "
    "Any tuning suggestion you give is a hypothesis read off the "
    "statement text, not a diagnosis: you cannot see the execution "
    "plan, table sizes, or existing indexes, so frame suggestions as "
    "what to check. When sql_text_truncated is true the statement's "
    "tail is missing -- do not claim a WHERE clause or LIMIT is "
    "absent when you may simply not be seeing it; set confidence to "
    "'low' and say what would need to be confirmed. "
    "If a resource has no Top SQL data for the window, say so "
    "instead of guessing. Answer in Korean, concise and "
    "DBA-oriented, and call the "
    f"{_TOOL_NAME} tool to deliver the result."
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
            total_aas = round(
                sum(
                    float(
                        sql_row[
                            "avg_active_sessions"
                        ]
                        or 0.0
                    )
                    for sql_row in top_sql_rows
                ),
                4,
            )

            lines.append(
                f"[{resource_id}] Top SQL total "
                f"avg_active_sessions={total_aas} "
                f"across {len(top_sql_rows)} "
                "ranked statements"
            )

            for rank, sql_row in enumerate(
                top_sql_rows, start=1
            ):
                lines.append(
                    f"[{resource_id}] Top SQL #{rank}: "
                    "avg_active_sessions="
                    f"{sql_row['avg_active_sessions']}, "
                    f"sql_id={sql_row['sql_id']}, "
                    "sql_text_truncated="
                    f"{bool(sql_row.get('sql_text_truncated'))}, "
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
    dict[str, Any],
]:
    """Fetch live health + Top SQL for each resource and return an AI estimate.

    `target_session` is reused for both CloudWatch and Performance
    Insights calls, since both run against the same target-account
    role. `dbi_resource_id_by_resource_id` should already be filtered
    to resources with Performance Insights enabled (see
    src.query.top_sql.resolve_pi_dbi_resource_ids) -- a resource_id
    with no entry there simply gets no Top SQL data in this summary.

    The third return value is the analysis: `{"overall": str,
    "queries": [...]}`, where each query entry carries the
    resource_id/rank it annotates so a caller can render it against
    the statement it refers to.
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
            {
                "overall": (
                    "해당 기간에 Performance Insights에 기록된 "
                    "SQL 활동이 없습니다."
                ),
                "queries": [],
            },
        )

    user_prompt = _format_prompt(
        health_rows=health_rows,
        top_sql_by_resource=top_sql_by_resource,
        lookback_minutes=lookback_minutes,
    )

    tool_input = invoke_tool(
        session=bedrock_session,
        model_id=model_id,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        tool_spec=_TOOL_SPEC,
        max_tokens=_MAX_ANALYSIS_TOKENS,
    )

    return (
        health_rows,
        top_sql_by_resource,
        _normalize_analysis(tool_input),
    )


def _normalize_analysis(
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    """Coerce the model's tool input into the shape callers render.

    The tool schema is advisory rather than enforced, so entries can
    come back missing a rank or carrying a rank as a string. Anything
    that cannot be tied back to a specific ranked statement is dropped
    rather than rendered against the wrong query.
    """

    overall = str(
        tool_input.get("overall") or ""
    ).strip()

    queries: list[dict[str, Any]] = []

    for entry in tool_input.get("queries") or []:
        if not isinstance(entry, dict):
            continue

        resource_id = str(
            entry.get("resource_id") or ""
        ).strip()

        try:
            rank = int(entry.get("rank"))
        except (TypeError, ValueError):
            continue

        if not resource_id or rank < 1:
            continue

        finding = str(
            entry.get("finding") or ""
        ).strip()

        if not finding:
            continue

        queries.append(
            {
                "resource_id": resource_id,
                "rank": rank,
                "finding": finding,
                "suggestion": str(
                    entry.get("suggestion") or ""
                ).strip(),
                "confidence": str(
                    entry.get("confidence")
                    or "low"
                ).strip(),
            }
        )

    return {
        "overall": overall,
        "queries": queries,
    }


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
            analysis,
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

    findings_by_key = {
        (
            entry["resource_id"],
            entry["rank"],
        ): entry
        for entry in analysis["queries"]
    }

    for resource_id, sql_rows in (
        top_sql_by_resource.items()
    ):
        print(f"[{resource_id}] Top SQL:")

        for rank, sql_row in enumerate(
            sql_rows, start=1
        ):
            truncated_note = (
                " (truncated)"
                if sql_row.get(
                    "sql_text_truncated"
                )
                else ""
            )

            print(
                f"  #{rank} AAS="
                f"{sql_row['avg_active_sessions']} "
                f"sql_id={sql_row['sql_id']}"
                f"{truncated_note}"
            )
            print(
                f"      {sql_row['sql_text']}"
            )

            entry = findings_by_key.get(
                (resource_id, rank)
            )

            if entry:
                print(
                    f"      -> {entry['finding']}"
                    f" [{entry['confidence']}]"
                )

                if entry["suggestion"]:
                    print(
                        "         "
                        f"{entry['suggestion']}"
                    )

    print()
    print("-" * 60)
    print(analysis["overall"])


if __name__ == "__main__":
    main()
