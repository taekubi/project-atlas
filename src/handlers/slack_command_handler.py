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
import time
import urllib.parse
import urllib.request
from datetime import (
    datetime,
    timedelta,
    timezone,
)
from pathlib import Path
from typing import Any

import boto3

from src.ai.bedrock_client import (
    BedrockInvocationError,
    create_session as create_bedrock_session,
)
from src.ai.db_health_summary import (
    summarize_db_health,
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
_KST = timezone(timedelta(hours=9))


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


def _today_kst() -> str:
    """Return today's date in KST as YYYY-MM-DD."""

    return datetime.now(_KST).strftime(
        "%Y-%m-%d"
    )


def parse_command_text(
    text: str,
) -> tuple[str, str]:
    """Parse '/atlas health <target> [YYYY-MM-DD]' into (target, date)."""

    parts = (text or "").strip().split()

    if len(parts) < 2 or parts[0] != "health":
        raise SlackCommandError(
            "사용법: /atlas health <target> [YYYY-MM-DD]"
        )

    target_name = parts[1]
    date = (
        parts[2]
        if len(parts) > 2
        else _today_kst()
    )

    return target_name, date


def resolve_target(
    config: AtlasConfig,
    target_name: str,
) -> TargetSettings:
    """Look up an enabled Atlas target by its configured name."""

    for target in config.enabled_targets:
        if target.name == target_name:
            return target

    raise SlackCommandError(
        f"등록되지 않은 target입니다: {target_name}"
    )


def format_slack_message(
    target_name: str,
    date: str,
    rows: list[dict[str, str | None]],
    summary: str,
) -> str:
    """Render a DB Health Summary as a Slack mrkdwn message."""

    if not rows:
        return (
            f"*{target_name}* ({date}) 조회 결과가 없습니다."
        )

    return "\n".join(
        [
            f"*{target_name} DB Health* ({date})",
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
    """Verify, parse, and dispatch a /atlas slash command."""

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

    form = urllib.parse.parse_qs(body)
    command_text = form.get("text", [""])[0]
    response_url = form.get(
        "response_url", [""]
    )[0]

    try:
        target_name, date = parse_command_text(
            command_text
        )
    except SlackCommandError as error:
        return _json_response(
            200,
            {
                "response_type": "ephemeral",
                "text": str(error),
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
                "target_name": target_name,
                "date": date,
                "response_url": response_url,
            }
        ).encode("utf-8"),
    )

    return _json_response(
        200,
        {
            "response_type": "ephemeral",
            "text": (
                f"{target_name} 상태를 조회하고 있습니다... "
                f"({date})"
            ),
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
    """Run the DB Health Summary and post the result to Slack."""

    target_name = event["target_name"]
    date = event["date"]
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
        config = _download_config(
            bucket_name=bucket_name,
            object_key=config_key,
            storage_region=storage_region,
            local_path=Path(
                "/tmp/atlas.toml"
            ),
        )

        target = resolve_target(
            config, target_name
        )

        athena_session = create_athena_session(
            profile_name=None,
            region_name=storage_region,
        )
        bedrock_session = create_bedrock_session(
            profile_name=None,
            region_name=bedrock_region,
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
            model_id=model_id,
            database=athena_database,
            table=athena_table,
            workgroup=athena_workgroup,
        )

        text = format_slack_message(
            target_name=target_name,
            date=date,
            rows=rows,
            summary=summary,
        )

    except (
        SlackCommandError,
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
