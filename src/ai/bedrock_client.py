"""Invoke Amazon Bedrock models for Project Atlas AI interpretation."""

from __future__ import annotations

import argparse
import sys

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ProfileNotFound,
)

_DEFAULT_MAX_TOKENS = 1024
_DEFAULT_TEMPERATURE = 0.2


class BedrockInvocationError(Exception):
    """Raised when a Bedrock model invocation fails or returns no text."""


def create_session(
    profile_name: str | None,
    region_name: str,
) -> boto3.Session:
    """Create an AWS session for local or Lambda execution."""

    if profile_name:
        return boto3.Session(
            profile_name=profile_name,
            region_name=region_name,
        )

    return boto3.Session(
        region_name=region_name,
    )


def invoke_model(
    session: boto3.Session,
    model_id: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
) -> str:
    """Send a single-turn prompt to a Bedrock model and return its reply text."""

    client = session.client("bedrock-runtime")

    response = client.converse(
        modelId=model_id,
        system=[
            {
                "text": system_prompt,
            }
        ],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "text": user_prompt,
                    }
                ],
            }
        ],
        inferenceConfig={
            "maxTokens": max_tokens,
            "temperature": temperature,
        },
    )

    content_blocks = response["output"]["message"]["content"]

    text_blocks = [
        block["text"]
        for block in content_blocks
        if "text" in block
    ]

    if not text_blocks:
        raise BedrockInvocationError(
            "Bedrock response contained no text content"
        )

    return "\n".join(text_blocks)


def invoke_tool(
    session: boto3.Session,
    model_id: str,
    system_prompt: str,
    user_prompt: str,
    tool_spec: dict,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    temperature: float = _DEFAULT_TEMPERATURE,
) -> dict:
    """Send a prompt with a forced tool call and return the tool's input.

    Used for structured extraction (e.g. parsing free-form text into
    query parameters) rather than free-text replies -- the model is
    required to answer by calling the given tool, so the result is a
    plain dict matching the tool's input schema instead of prose to
    parse.
    """

    client = session.client("bedrock-runtime")

    tool_name = tool_spec["toolSpec"]["name"]

    response = client.converse(
        modelId=model_id,
        system=[
            {
                "text": system_prompt,
            }
        ],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "text": user_prompt,
                    }
                ],
            }
        ],
        toolConfig={
            "tools": [tool_spec],
            "toolChoice": {
                "tool": {
                    "name": tool_name,
                },
            },
        },
        inferenceConfig={
            "maxTokens": max_tokens,
            "temperature": temperature,
        },
    )

    content_blocks = response["output"]["message"]["content"]

    for block in content_blocks:
        tool_use = block.get("toolUse")

        if (
            tool_use
            and tool_use.get("name") == tool_name
        ):
            return tool_use.get("input", {})

    raise BedrockInvocationError(
        "Bedrock response did not include a "
        f"{tool_name} tool call"
    )


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Send an ad-hoc prompt to an "
            "Amazon Bedrock model for "
            "Project Atlas."
        )
    )

    parser.add_argument(
        "--profile",
        default="atlas-test",
        help="AWS CLI profile",
    )
    parser.add_argument(
        "--region",
        default="ap-northeast-2",
        help="AWS Region running Bedrock",
    )
    parser.add_argument(
        "--model-id",
        required=True,
        help="Bedrock model ID or inference profile ID",
    )
    parser.add_argument(
        "--system-prompt",
        default="You are a helpful assistant.",
        help="System prompt",
    )
    parser.add_argument(
        "--prompt",
        required=True,
        help="User prompt",
    )

    return parser.parse_args()


def main() -> None:
    """Run the Bedrock ad-hoc prompt CLI."""

    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="replace",
    )

    args = parse_arguments()

    try:
        session = create_session(
            profile_name=args.profile,
            region_name=args.region,
        )

        reply = invoke_model(
            session=session,
            model_id=args.model_id,
            system_prompt=args.system_prompt,
            user_prompt=args.prompt,
        )

    except ProfileNotFound as error:
        raise SystemExit(
            f"AWS profile not found: {error}"
        ) from error
    except BedrockInvocationError as error:
        raise SystemExit(str(error)) from error
    except (
        ClientError,
        BotoCoreError,
    ) as error:
        raise SystemExit(
            f"Bedrock invocation failed: {error}"
        ) from error

    print(reply)


if __name__ == "__main__":
    main()
