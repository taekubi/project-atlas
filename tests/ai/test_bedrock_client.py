"""Tests for src.ai.bedrock_client (no real Bedrock calls)."""

from unittest.mock import MagicMock, patch

import pytest

from src.ai.bedrock_client import (
    BedrockInvocationError,
    create_session,
    invoke_model,
    invoke_tool,
)

_TOOL_SPEC = {
    "toolSpec": {
        "name": "resolve_query",
        "description": "test tool",
        "inputSchema": {
            "json": {"type": "object"}
        },
    }
}


def _session_with_client():
    client = MagicMock()
    session = MagicMock()
    session.client.return_value = client
    return session, client


# --- create_session -----------------------------------------------------


def test_create_session_uses_the_given_profile():
    with patch(
        "src.ai.bedrock_client.boto3.Session"
    ) as session_cls:
        create_session(
            profile_name="atlas-test",
            region_name="ap-northeast-2",
        )

    session_cls.assert_called_once_with(
        profile_name="atlas-test",
        region_name="ap-northeast-2",
    )


def test_create_session_omits_profile_when_none():
    # Lambda has no named profile -- passing profile_name=None to
    # boto3.Session would be a different (still valid) call shape than
    # simply not passing it, so the two paths are kept distinct.
    with patch(
        "src.ai.bedrock_client.boto3.Session"
    ) as session_cls:
        create_session(
            profile_name=None,
            region_name="ap-northeast-2",
        )

    session_cls.assert_called_once_with(
        region_name="ap-northeast-2",
    )


# --- invoke_model ---------------------------------------------------------


def test_invoke_model_returns_the_response_text():
    session, client = _session_with_client()
    client.converse.return_value = {
        "output": {
            "message": {
                "content": [
                    {"text": "요약 결과입니다."}
                ]
            }
        }
    }

    result = invoke_model(
        session=session,
        model_id="model-1",
        system_prompt="system",
        user_prompt="user",
    )

    assert result == "요약 결과입니다."


def test_invoke_model_joins_multiple_text_blocks():
    session, client = _session_with_client()
    client.converse.return_value = {
        "output": {
            "message": {
                "content": [
                    {"text": "첫 줄"},
                    {"text": "둘째 줄"},
                ]
            }
        }
    }

    result = invoke_model(
        session=session,
        model_id="model-1",
        system_prompt="system",
        user_prompt="user",
    )

    assert result == "첫 줄\n둘째 줄"


def test_invoke_model_raises_when_no_text_block_is_present():
    session, client = _session_with_client()
    client.converse.return_value = {
        "output": {"message": {"content": []}}
    }

    with pytest.raises(
        BedrockInvocationError
    ):
        invoke_model(
            session=session,
            model_id="model-1",
            system_prompt="system",
            user_prompt="user",
        )


def test_invoke_model_sends_the_prompts_and_inference_config():
    session, client = _session_with_client()
    client.converse.return_value = {
        "output": {
            "message": {
                "content": [{"text": "ok"}]
            }
        }
    }

    invoke_model(
        session=session,
        model_id="model-1",
        system_prompt="시스템 프롬프트",
        user_prompt="사용자 질문",
        max_tokens=2048,
        temperature=0.1,
    )

    call = client.converse.call_args.kwargs
    assert call["modelId"] == "model-1"
    assert call["system"] == [
        {"text": "시스템 프롬프트"}
    ]
    assert call["messages"] == [
        {
            "role": "user",
            "content": [
                {"text": "사용자 질문"}
            ],
        }
    ]
    assert call["inferenceConfig"] == {
        "maxTokens": 2048,
        "temperature": 0.1,
    }


# --- invoke_tool ---------------------------------------------------------


def test_invoke_tool_returns_the_tool_input():
    session, client = _session_with_client()
    client.converse.return_value = {
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": "resolve_query",
                            "input": {
                                "target": "watchcon-a",
                                "mode": "live",
                            },
                        }
                    }
                ]
            }
        }
    }

    result = invoke_tool(
        session=session,
        model_id="model-1",
        system_prompt="system",
        user_prompt="watchcon-a 상태",
        tool_spec=_TOOL_SPEC,
    )

    assert result == {
        "target": "watchcon-a",
        "mode": "live",
    }


def test_invoke_tool_forces_the_named_tool():
    session, client = _session_with_client()
    client.converse.return_value = {
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": "resolve_query",
                            "input": {},
                        }
                    }
                ]
            }
        }
    }

    invoke_tool(
        session=session,
        model_id="model-1",
        system_prompt="system",
        user_prompt="user",
        tool_spec=_TOOL_SPEC,
    )

    call = client.converse.call_args.kwargs
    assert call["toolConfig"] == {
        "tools": [_TOOL_SPEC],
        "toolChoice": {
            "tool": {"name": "resolve_query"}
        },
    }


def test_invoke_tool_ignores_a_differently_named_tool_use_block():
    session, client = _session_with_client()
    client.converse.return_value = {
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": "some_other_tool",
                            "input": {"x": 1},
                        }
                    }
                ]
            }
        }
    }

    with pytest.raises(
        BedrockInvocationError
    ):
        invoke_tool(
            session=session,
            model_id="model-1",
            system_prompt="system",
            user_prompt="user",
            tool_spec=_TOOL_SPEC,
        )


def test_invoke_tool_raises_when_no_tool_use_block_is_present():
    session, client = _session_with_client()
    client.converse.return_value = {
        "output": {
            "message": {
                "content": [
                    {"text": "그냥 대답"}
                ]
            }
        }
    }

    with pytest.raises(
        BedrockInvocationError
    ):
        invoke_tool(
            session=session,
            model_id="model-1",
            system_prompt="system",
            user_prompt="user",
            tool_spec=_TOOL_SPEC,
        )


def test_invoke_tool_defaults_a_missing_input_to_an_empty_dict():
    session, client = _session_with_client()
    client.converse.return_value = {
        "output": {
            "message": {
                "content": [
                    {
                        "toolUse": {
                            "name": "resolve_query"
                        }
                    }
                ]
            }
        }
    }

    result = invoke_tool(
        session=session,
        model_id="model-1",
        system_prompt="system",
        user_prompt="user",
        tool_spec=_TOOL_SPEC,
    )

    assert result == {}
