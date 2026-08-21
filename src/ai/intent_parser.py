"""Parse free-form /atlas requests into structured query parameters.

Complements the fixed "health <target> [date|Nm|Nh]", "storage
<target> [Nd]", and "topsql <target> [Nm]" grammars in
src.handlers.slack_command_handler.parse_command_text: that fast,
free, deterministic parser is tried first; this Bedrock-based parser
is the fallback for genuine natural language, e.g. "watchcon-a 최근
30분 상태 확인해줘", "watchcon-a 스토리지 얼마나 남았어?", or
"watchcon-a 지금 가장 부하 큰 SQL이 뭐야?". Both return the same
(target_name, mode, value) shape so downstream handling does not need
to know which parser was used.
"""

from __future__ import annotations

import boto3

from src.ai.bedrock_client import invoke_tool
from src.query.monthly_report import (
    default_report_month,
)
from src.query.validators import (
    MONTH_PATTERN,
)

_DEFAULT_MODEL_ID = (
    "apac.anthropic.claude-3-5-sonnet-20241022-v2:0"
)

_DEFAULT_LOOKBACK_MINUTES = 30
_DEFAULT_STORAGE_LOOKBACK_DAYS = 30
_DEFAULT_TOPSQL_LOOKBACK_MINUTES = 10

_TOOL_NAME = "resolve_db_health_query"

_TOOL_SPEC = {
    "toolSpec": {
        "name": _TOOL_NAME,
        "description": (
            "Extract the target database/cluster and the desired time "
            "window from a Korean or English question about RDS/Aurora "
            "database health."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "The database, instance, or cluster name "
                            "mentioned in the request (e.g. watchcon-a, "
                            "watchcon, headquarters). Copy it exactly as "
                            "written; never invent a name that was not "
                            "mentioned."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": [
                            "live",
                            "date",
                            "storage",
                            "topsql",
                            "report",
                        ],
                        "description": (
                            "'live' for a recent/current-status question "
                            "(e.g. 최근 30분, 지금, 방금) or when no "
                            "explicit calendar date is given. 'date' only "
                            "when a specific calendar date is named. "
                            "'storage' for a storage/disk capacity "
                            "question (e.g. 스토리지 얼마나 남았어, "
                            "용량 며칠 뒤 소진돼, 디스크 꽉 차겠어) -- "
                            "anything asking about free space, capacity, "
                            "or when storage will run out. 'topsql' for "
                            "a question about which SQL/query is "
                            "causing load right now (e.g. 가장 많은 "
                            "이벤트를 발생시키는 SQL이 뭐야, top sql, "
                            "부하 원인이 뭐야, 뭐가 느려). 'report' "
                            "for a request covering a whole calendar "
                            "month or asking what changed over a month "
                            "(e.g. 지난달 리포트, 7월 운영 보고서, "
                            "이번 달 어땠어, 지난달이랑 비교해줘) -- "
                            "prefer 'report' over 'date' whenever a "
                            "month rather than a single day is meant."
                        ),
                    },
                    "lookback_minutes": {
                        "type": "integer",
                        "description": (
                            "Only when mode is 'live' or 'topsql': the "
                            "lookback window in minutes. Convert hours "
                            "to minutes (e.g. 1시간 -> 60). Default to "
                            "30 for 'live' or 10 for 'topsql' if the "
                            "request does not specify a window."
                        ),
                    },
                    "date": {
                        "type": "string",
                        "description": (
                            "Only when mode is 'date': the date as "
                            "YYYY-MM-DD."
                        ),
                    },
                    "lookback_days": {
                        "type": "integer",
                        "description": (
                            "Only when mode is 'storage': how many days "
                            "of history to fit the storage trend on. "
                            "Default to 30 if the request does not "
                            "specify a window."
                        ),
                    },
                    "month": {
                        "type": "string",
                        "description": (
                            "Only when mode is 'report': the month as "
                            "YYYY-MM. Resolve relative wording against "
                            "today's date (지난달 -> the previous "
                            "month, 이번 달 -> the current month). "
                            "Omit if the request names no month, and "
                            "the last complete month is used."
                        ),
                    },
                },
                "required": [
                    "target",
                    "mode",
                ],
            }
        },
    }
}

_SYSTEM_PROMPT = (
    "You turn a Slack request about an AWS RDS/Aurora database's health "
    f"into a structured query by calling the {_TOOL_NAME} tool. Never "
    "invent a target name the user did not mention. If you cannot "
    "identify any target at all, still call the tool with your best "
    "guess at the target text (even if empty) so the caller can report "
    "a clear error."
)


class IntentParseError(Exception):
    """Raised when free-form text cannot be resolved into a query."""


def parse_health_intent(
    bedrock_session: boto3.Session,
    text: str,
    model_id: str = _DEFAULT_MODEL_ID,
) -> tuple[str, str, str]:
    """Parse free-form text into (target_name, mode, value).

    `value` is a YYYY-MM-DD date for mode "date", or a lookback window in
    minutes (as a string) for mode "live" -- the same shape
    parse_command_text returns.
    """

    if not text or not text.strip():
        raise IntentParseError(
            "질문을 이해하지 못했습니다. "
            "예: 'watchcon-a 최근 30분 상태 확인해줘'"
        )

    tool_input = invoke_tool(
        session=bedrock_session,
        model_id=model_id,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=text,
        tool_spec=_TOOL_SPEC,
    )

    target_name = str(
        tool_input.get("target") or ""
    ).strip()

    if not target_name:
        raise IntentParseError(
            "어떤 DB/클러스터를 조회할지 이해하지 못했습니다. "
            "DB 이름을 포함해서 다시 질문해주세요."
        )

    if tool_input.get("mode") == "date":
        date = str(
            tool_input.get("date") or ""
        ).strip()

        if not date:
            raise IntentParseError(
                "조회할 날짜를 이해하지 못했습니다. "
                "YYYY-MM-DD 형식으로 다시 질문해주세요."
            )

        return target_name, "date", date

    if tool_input.get("mode") == "storage":
        lookback_days = tool_input.get(
            "lookback_days"
        )

        if (
            not isinstance(lookback_days, int)
            or lookback_days <= 0
        ):
            lookback_days = (
                _DEFAULT_STORAGE_LOOKBACK_DAYS
            )

        return (
            target_name,
            "storage",
            str(lookback_days),
        )

    if tool_input.get("mode") == "topsql":
        lookback_minutes = tool_input.get(
            "lookback_minutes"
        )

        if (
            not isinstance(
                lookback_minutes, int
            )
            or lookback_minutes <= 0
        ):
            lookback_minutes = (
                _DEFAULT_TOPSQL_LOOKBACK_MINUTES
            )

        return (
            target_name,
            "topsql",
            str(lookback_minutes),
        )

    if tool_input.get("mode") == "report":
        month = str(
            tool_input.get("month") or ""
        ).strip()

        # An unparseable or absent month falls back to the last
        # complete month rather than failing: "리포트 보여줘" with no
        # month named is a reasonable request, and that is the month it
        # almost always means.
        if not MONTH_PATTERN.match(month):
            month = default_report_month()

        return (
            target_name,
            "report",
            month,
        )

    lookback_minutes = tool_input.get(
        "lookback_minutes"
    )

    if (
        not isinstance(lookback_minutes, int)
        or lookback_minutes <= 0
    ):
        lookback_minutes = (
            _DEFAULT_LOOKBACK_MINUTES
        )

    return (
        target_name,
        "live",
        str(lookback_minutes),
    )
