"""Tests that handle_query_job always answers, however it fails.

A /atlas request is answered asynchronously: Slack has already been
acked, and the only way the user ever hears back is
_post_to_response_url. If handle_query_job raises instead, the user is
left with "질문을 확인하고 있습니다..." and no error -- indistinguishable
from a hang. These tests pin that contract for each class of failure.
"""

import os
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.collectors.resource_resolver import (
    ResourceResolutionError,
)
from src.handlers.slack_command_handler import (
    handle_query_job,
)

_ENV = {
    "ATLAS_BUCKET": "atlas-test-bucket",
    "ATLAS_STORAGE_REGION": "ap-northeast-2",
    "ATLAS_ATHENA_OUTPUT_LOCATION": "s3://atlas-test-bucket/athena/",
}

_EVENT = {
    "command_text": "health watchcon-a 30m",
    "response_url": "https://hooks.slack.test/response",
}


class _Context:
    aws_request_id = "test-request-id-0001"


def _client_error() -> ClientError:
    return ClientError(
        {
            "Error": {
                "Code": "ThrottlingException",
                "Message": "Rate exceeded",
            }
        },
        "Converse",
    )


def _run_with_failure(
    error: Exception,
    event: dict | None = None,
):
    """Run handle_query_job with config download raising `error`.

    _download_config is the first AWS call after the intent is parsed,
    so raising there exercises the handler's failure path without
    needing to stub the whole query pipeline.
    """

    posted: dict = {}

    def _capture(response_url, text):
        posted["response_url"] = response_url
        posted["text"] = text

    with patch.dict(os.environ, _ENV, clear=False), patch(
        "src.handlers.slack_command_handler."
        "create_bedrock_session",
        return_value=MagicMock(),
    ), patch(
        "src.handlers.slack_command_handler."
        "_download_config",
        side_effect=error,
    ), patch(
        "src.handlers.slack_command_handler."
        "_post_to_response_url",
        side_effect=_capture,
    ):
        result = handle_query_job(
            event if event is not None else _EVENT,
            _Context(),
        )

    return result, posted


def test_aws_client_error_still_answers_in_slack():
    # C-1: ClientError was previously uncaught, so a throttled Bedrock
    # call or a missing IAM permission produced no reply at all.
    result, posted = _run_with_failure(
        _client_error()
    )

    assert result == {"status": "sent"}
    assert posted["text"]
    assert "오류" in posted["text"]


def test_aws_failure_message_carries_request_id_not_raw_error():
    result, posted = _run_with_failure(
        _client_error()
    )

    assert "ThrottlingException" not in posted["text"]
    assert "ClientError" in posted["text"]
    assert (
        _Context.aws_request_id
        in posted["text"]
    )


def test_unexpected_exception_still_answers_in_slack():
    # Nothing in the handler's except tuple matches KeyError; the
    # catch-all must still turn it into a reply.
    result, posted = _run_with_failure(
        KeyError("instances")
    )

    assert result == {"status": "sent"}
    assert "KeyError" in posted["text"]


def test_user_error_shows_its_own_message():
    # A name the user can correct: the message is actionable, so it is
    # passed through rather than replaced with a request ID.
    result, posted = _run_with_failure(
        ResourceResolutionError(
            "'nope'과(와) 일치하는 DB/클러스터를 찾을 수 없습니다."
        )
    )

    assert result == {"status": "sent"}
    assert "조회 실패" in posted["text"]
    assert "nope" in posted["text"]
    assert (
        _Context.aws_request_id
        not in posted["text"]
    )


def test_missing_response_url_is_reported_not_raised():
    result, posted = _run_with_failure(
        _client_error(),
        event={
            "command_text": "health watchcon-a 30m",
            "response_url": "",
        },
    )

    assert result == {"status": "undeliverable"}
    assert posted == {}


def test_missing_required_env_still_answers_in_slack():
    # Environment lookups used to run before the try block, so a
    # misconfigured function died without telling anyone.
    posted: dict = {}

    def _capture(response_url, text):
        posted["text"] = text

    with patch.dict(
        os.environ,
        {"ATLAS_BUCKET": ""},
        clear=True,
    ), patch(
        "src.handlers.slack_command_handler."
        "_post_to_response_url",
        side_effect=_capture,
    ):
        result = handle_query_job(
            _EVENT, _Context()
        )

    assert result == {"status": "sent"}
    assert "ValueError" in posted["text"]


def test_slack_delivery_failure_does_not_raise():
    # The answer exists but cannot be delivered (expired response_url).
    # Raising here would fail the invocation and make Lambda retry the
    # entire query, so it is logged and reported instead.
    with patch.dict(os.environ, _ENV, clear=False), patch(
        "src.handlers.slack_command_handler."
        "create_bedrock_session",
        return_value=MagicMock(),
    ), patch(
        "src.handlers.slack_command_handler."
        "_download_config",
        side_effect=_client_error(),
    ), patch(
        "src.handlers.slack_command_handler."
        "_post_to_response_url",
        side_effect=OSError("connection reset"),
    ):
        result = handle_query_job(
            _EVENT, _Context()
        )

    assert result == {
        "status": "delivery_failed"
    }
