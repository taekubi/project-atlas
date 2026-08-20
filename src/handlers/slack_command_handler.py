"""AWS Lambda handlers for the Project Atlas /atlas Slack slash command.

Slack requires an acknowledgement within 3 seconds, but an Athena query
plus a Bedrock interpretation routinely takes longer than that. So this
module is split into two Lambda entry points sharing one deployment
package:

- handle_slash_command: verifies the Slack signature, parses the command,
  asynchronously invokes the job function, and immediately acknowledges.
- handle_query_job: runs the actual DB Health Summary and posts the result
  back to Slack's response_url.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import boto3

from src.ai.bedrock_client import (
    BedrockInvocationError,
    create_session as create_bedrock_session,
)
from src.ai.db_health_summary import (
    summarize_db_health,
    summarize_live_db_health,
)
from src.ai.intent_parser import (
    IntentParseError,
    parse_health_intent,
)
from src.auth.aws_session import (
    create_target_session,
)
from src.config.atlas_config import (
    AtlasConfig,
    TargetSettings,
    load_config,
)
from src.query.athena_client import (
    AthenaQueryError,
    create_session as create_athena_session,
)
_SIGNATURE_VERSION = "v0"
_MAX_REQUEST_AGE_SECONDS = 300
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DURATION_PATTERN = re.compile(r"^(\d+)(m|h)$")
_DEFAULT_LOOKBACK_MINUTES = 30


class SlackSignatureError(Exception):
    """Raised when a Slack request signature cannot be verified."""


class SlackCommandError(Exception):
    """Raised when a /atlas command cannot be parsed or resolved."""


def _required_env(name: str) -> str:
    """Return a required environment variable."""

    value = os.getenv(name)

    if value is None or not value.strip():
        raise ValueError(
            f"Missing required environment variable: {name}"
        )

    return value.strip()


def verify_slack_signature(
    signing_secret: str,
    timestamp: str,
    body: str,
    provided_signature: str,
) -> None:
    """Verify a Slack request signature, raising if it does not match."""

    try:
        request_time = int(timestamp)
    except (TypeError, ValueError) as error:
        raise SlackSignatureError(
            "Missing or invalid Slack timestamp"
        ) from error

    if (
        abs(time.time() - request_time)
        > _MAX_REQUEST_AGE_SECONDS
    ):
        raise SlackSignatureError(
            "Slack request timestamp is too old"
        )

    basestring = (
        f"{_SIGNATURE_VERSION}:{timestamp}:{body}"
    )

    computed_hash = hmac.new(
        signing_secret.encode("utf-8"),
        basestring.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    computed_signature = (
        f"{_SIGNATURE_VERSION}={computed_hash}"
    )

    if not hmac.compare_digest(
        computed_signature,
        provided_signature or "",
    ):
        raise SlackSignatureError(
            "Slack signature mismatch"
        )


def parse_command_text(
    text: str,
) -> tuple[str, str, str]:
    """Parse '/atlas health <target> [YYYY-MM-DD | Nm | Nh]'.

    Returns (target_name, mode, value):
    - mode "date": value is a YYYY-MM-DD date -- historical, reads the
      Curated/Athena layer (day granularity, batch latency)
    - mode "live": value is a lookback window in minutes (as a string) --
      reads CloudWatch directly. This is the default when no third
      argument is given, since fast monitoring is the primary use case.
    """

    parts = (text or "").strip().split()

    if len(parts) < 2 or parts[0] != "health":
        raise SlackCommandError(
            "사용법: /atlas health <target> [YYYY-MM-DD | 30m | 2h]"
        )

    target_name = parts[1]

    if len(parts) <= 2:
        return (
            target_name,
            "live",
            str(_DEFAULT_LOOKBACK_MINUTES),
        )

    argument = parts[2]

    if _DATE_PATTERN.match(argument):
        return target_name, "date", argument

    duration_match = _DURATION_PATTERN.match(
        argument
    )

    if duration_match:
        amount, unit = duration_match.groups()
        minutes = int(amount) * (
            60 if unit == "h" else 1
        )
        return (
            target_name,
            "live",
            str(minutes),
        )

    raise SlackCommandError(
        "날짜(YYYY-MM-DD) 또는 기간(예: 30m, 2h) 형식으로 입력해주세요."
    )


def resolve_query_scope(
    config: AtlasConfig,
    name: str,
) -> tuple[TargetSettings, str | None]:
    """Resolve a /atlas argument to a target account and optional resource.

    If `name` matches a configured Atlas target's name, the command asks
    for every resource in that account/region. Otherwise `name` is treated
    as a resource_id (e.g. an RDS/Aurora identifier like "watchcon-a") and
    scoped to Atlas's first enabled target -- there is only one configured
    account today, so this does not search across multiple accounts; revisit
    if that changes.
    """

    for target in config.enabled_targets:
        if target.name == name:
            return target, None

    if not config.enabled_targets:
        raise SlackCommandError(
            "등록된 target이 없습니다."
        )

    return config.enabled_targets[0], name


def format_slack_message(
    target_name: str,
    label: str,
    rows: list[dict[str, str | None]],
    summary: str,
) -> str:
    """Render a DB Health Summary as a Slack mrkdwn message.

    `label` describes the query window shown next to the target name --
    a date (historical) or a phrase like "최근 30분" (live).
    """

    if not rows:
        return (
            f"*{target_name}* ({label}) 조회 결과가 없습니다."
        )

    return "\n".join(
        [
            f"*{target_name} DB Health* ({label})",
            "",
            summary,
        ]
    )


def _json_response(
    status_code: int,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Build an API Gateway / Function URL proxy response."""

    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
        },
        "body": json.dumps(body),
    }


def _decode_body(
    event: dict[str, Any],
) -> str:
    """Decode the raw Slack request body from a proxy event."""

    body = event.get("body", "") or ""

    if event.get("isBase64Encoded"):
        return base64.b64decode(body).decode(
            "utf-8"
        )

    return body


def _get_slack_signing_secret() -> str:
    """Fetch the Slack signing secret from SSM Parameter Store.

    Stored as a SecureString rather than a plain Lambda environment
    variable so it never appears in the function configuration or in
    deployment tooling output.
    """

    parameter_name = os.getenv(
        "SLACK_SIGNING_SECRET_PARAM",
        "/project-atlas/slack/signing-secret",
    ).strip()

    ssm = boto3.client("ssm")

    response = ssm.get_parameter(
        Name=parameter_name,
        WithDecryption=True,
    )

    return response["Parameter"]["Value"]


def handle_slash_command(
    event: dict[str, Any],
    context: Any,
) -> dict[str, Any]:
    """Verify a /atlas slash command and dispatch it for processing.

    Parsing (the fixed grammar, and its Bedrock-based natural-language
    fallback) happens entirely in handle_query_job, not here -- a
    synchronous Bedrock call here would risk missing Slack's 3-second
    ack window on top of Lambda cold-start latency. This handler only
    verifies the request and hands the raw text off.
    """

    signing_secret = (
        _get_slack_signing_secret()
    )

    headers = {
        key.lower(): value
        for key, value in (
            event.get("headers") or {}
        ).items()
    }

    body = _decode_body(event)

    verify_slack_signature(
        signing_secret=signing_secret,
        timestamp=headers.get(
            "x-slack-request-timestamp", ""
        ),
        body=body,
        provided_signature=headers.get(
            "x-slack-signature", ""
        ),
    )

    if headers.get("x-slack-retry-num"):
        # Slack retried because it didn't see our ack within its
        # 3-second window (cold start + cross-Pacific network latency
        # can exceed that even when our own Lambda duration is fine).
        # The original invocation already dispatched the job -- or is
        # about to -- so ack this retry without dispatching a
        # duplicate one, which would double the Bedrock/Athena cost
        # and could post two answers to the same thread.
        return _json_response(
            200,
            {
                "response_type": "ephemeral",
                "text": "질문을 확인하고 있습니다...",
            },
        )

    form = urllib.parse.parse_qs(body)
    command_text = form.get("text", [""])[0]
    response_url = form.get(
        "response_url", [""]
    )[0]

    if not command_text.strip():
        return _json_response(
            200,
            {
                "response_type": "ephemeral",
                "text": (
                    "사용법: /atlas <DB 이름> [질문] "
                    "(예: /atlas watchcon-a 최근 30분 "
                    "상태 확인해줘, 또는 /atlas health "
                    "watchcon-a 2026-08-19)"
                ),
            },
        )

    job_function_name = _required_env(
        "ATLAS_SLACK_JOB_FUNCTION"
    )

    lambda_client = boto3.client("lambda")

    lambda_client.invoke(
        FunctionName=job_function_name,
        InvocationType="Event",
        Payload=json.dumps(
            {
                "command_text": command_text,
                "response_url": response_url,
            }
        ).encode("utf-8"),
    )

    return _json_response(
        200,
        {
            "response_type": "ephemeral",
            "text": "질문을 확인하고 있습니다...",
        },
    )


def _download_config(
    bucket_name: str,
    object_key: str,
    storage_region: str,
    local_path: Path,
) -> AtlasConfig:
    """Download Atlas TOML configuration from Amazon S3."""

    local_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    session = boto3.Session(
        region_name=storage_region,
    )

    s3 = session.client("s3")

    s3.download_file(
        Bucket=bucket_name,
        Key=object_key,
        Filename=str(local_path),
    )

    return load_config(local_path)


def _post_to_response_url(
    response_url: str,
    text: str,
) -> None:
    """Post the final answer back to Slack via response_url."""

    payload = json.dumps(
        {
            "response_type": "ephemeral",
            "text": text,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        response_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(
        request,
        timeout=10,
    ) as response:
        response.read()


def handle_query_job(
    event: dict[str, Any],
    context: Any,
) -> dict[str, Any]:
    """Parse a /atlas request, run the DB Health Summary, and reply.

    Parses `command_text` with the fixed "health <target> [...]" grammar
    first; if that does not match, falls back to Bedrock-based natural
    -language parsing (src.ai.intent_parser) so free-form questions like
    "watchcon-a 최근 30분 상태 확인해줘" work too.
    """

    command_text = event["command_text"]
    response_url = event["response_url"]

    bucket_name = _required_env(
        "ATLAS_BUCKET"
    )
    storage_region = _required_env(
        "ATLAS_STORAGE_REGION"
    )
    athena_output_location = _required_env(
        "ATLAS_ATHENA_OUTPUT_LOCATION"
    )

    athena_database = os.getenv(
        "ATLAS_ATHENA_DATABASE",
        "project_atlas",
    ).strip()
    athena_table = os.getenv(
        "ATLAS_ATHENA_TABLE",
        "cloudwatch_metrics",
    ).strip()
    athena_workgroup = os.getenv(
        "ATLAS_ATHENA_WORKGROUP",
        "primary",
    ).strip()
    bedrock_region = os.getenv(
        "ATLAS_BEDROCK_REGION",
        storage_region,
    ).strip()
    model_id = os.getenv(
        "ATLAS_BEDROCK_MODEL_ID",
        "apac.anthropic.claude-3-5-sonnet-20241022-v2:0",
    ).strip()
    config_key = os.getenv(
        "ATLAS_CONFIG_KEY",
        "config/atlas.toml",
    ).strip()

    try:
        bedrock_session = create_bedrock_session(
            profile_name=None,
            region_name=bedrock_region,
        )

        try:
            target_name, mode, value = (
                parse_command_text(
                    command_text
                )
            )
        except SlackCommandError:
            target_name, mode, value = (
                parse_health_intent(
                    bedrock_session=(
                        bedrock_session
                    ),
                    text=command_text,
                    model_id=model_id,
                )
            )

        config = _download_config(
            bucket_name=bucket_name,
            object_key=config_key,
            storage_region=storage_region,
            local_path=Path(
                "/tmp/atlas.toml"
            ),
        )

        target, resource_filter = (
            resolve_query_scope(
                config, target_name
            )
        )

        if mode == "live":
            if resource_filter is None:
                raise SlackCommandError(
                    "실시간 조회는 계정 전체가 아니라 "
                    "개별 리소스 ID로만 가능합니다. "
                    "예: /atlas health watchcon-a 30m"
                )

            lookback_minutes = int(value)

            base_session = boto3.Session(
                region_name=target.regions[0],
            )
            cloudwatch_session = (
                create_target_session(
                    base_session=base_session,
                    region_name=target.regions[0],
                    role_arn=target.role_arn,
                )
            )
            athena_session = (
                create_athena_session(
                    profile_name=None,
                    region_name=storage_region,
                )
            )

            rows, summary = (
                summarize_live_db_health(
                    cloudwatch_session=(
                        cloudwatch_session
                    ),
                    athena_session=(
                        athena_session
                    ),
                    bedrock_session=(
                        bedrock_session
                    ),
                    output_location=(
                        athena_output_location
                    ),
                    account_id=target.account_id,
                    region=target.regions[0],
                    resource_id=resource_filter,
                    lookback_minutes=(
                        lookback_minutes
                    ),
                    model_id=model_id,
                    database=athena_database,
                    table=athena_table,
                    workgroup=athena_workgroup,
                )
            )

            label = f"최근 {lookback_minutes}분"

        else:
            date = value

            athena_session = (
                create_athena_session(
                    profile_name=None,
                    region_name=storage_region,
                )
            )

            rows, summary = summarize_db_health(
                athena_session=athena_session,
                bedrock_session=bedrock_session,
                output_location=(
                    athena_output_location
                ),
                account_id=target.account_id,
                region=target.regions[0],
                date=date,
                resource_id=resource_filter,
                model_id=model_id,
                database=athena_database,
                table=athena_table,
                workgroup=athena_workgroup,
            )

            label = date

        text = format_slack_message(
            target_name=target_name,
            label=label,
            rows=rows,
            summary=summary,
        )

    except (
        SlackCommandError,
        IntentParseError,
        AthenaQueryError,
        BedrockInvocationError,
        ValueError,
    ) as error:
        text = f"조회 실패: {error}"

    _post_to_response_url(
        response_url=response_url,
        text=text,
    )

    return {"status": "sent"}
