"""Tests for src.auth.aws_session (no real AWS calls)."""

from unittest.mock import MagicMock, patch

import pytest

from src.auth.aws_session import (
    create_target_session,
    get_session_account_id,
)


def _base_session_with_sts():
    sts = MagicMock()
    base_session = MagicMock()
    base_session.client.return_value = sts
    return base_session, sts


# --- create_target_session -----------------------------------------------


def test_create_target_session_returns_base_session_when_role_arn_is_none():
    base_session, sts = _base_session_with_sts()

    result = create_target_session(
        base_session=base_session,
        region_name="ap-northeast-2",
        role_arn=None,
    )

    assert result is base_session
    sts.assume_role.assert_not_called()


def test_create_target_session_returns_base_session_when_role_arn_is_blank():
    base_session, sts = _base_session_with_sts()

    result = create_target_session(
        base_session=base_session,
        region_name="ap-northeast-2",
        role_arn="   ",
    )

    assert result is base_session
    sts.assume_role.assert_not_called()


def test_create_target_session_rejects_a_blank_session_name():
    base_session, _ = _base_session_with_sts()

    with pytest.raises(
        ValueError,
        match="role_session_name",
    ):
        create_target_session(
            base_session=base_session,
            region_name="ap-northeast-2",
            role_arn="arn:aws:iam::826846563965:role/observer",
            role_session_name="   ",
        )


def test_create_target_session_assumes_the_role_with_a_stripped_arn():
    base_session, sts = _base_session_with_sts()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "AKIA...",
            "SecretAccessKey": "secret",
            "SessionToken": "token",
        }
    }

    with patch(
        "src.auth.aws_session.boto3.Session"
    ) as session_cls:
        create_target_session(
            base_session=base_session,
            region_name="ap-northeast-2",
            role_arn="  arn:aws:iam::826846563965:role/observer  ",
        )

    sts.assume_role.assert_called_once_with(
        RoleArn="arn:aws:iam::826846563965:role/observer",
        RoleSessionName="project-atlas-cloudwatch",
    )
    session_cls.assert_called_once_with(
        aws_access_key_id="AKIA...",
        aws_secret_access_key="secret",
        aws_session_token="token",
        region_name="ap-northeast-2",
    )


def test_create_target_session_omits_external_id_when_not_given():
    base_session, sts = _base_session_with_sts()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "a",
            "SecretAccessKey": "b",
            "SessionToken": "c",
        }
    }

    with patch(
        "src.auth.aws_session.boto3.Session"
    ):
        create_target_session(
            base_session=base_session,
            region_name="ap-northeast-2",
            role_arn="arn:aws:iam::826846563965:role/observer",
        )

    assert (
        "ExternalId"
        not in sts.assume_role.call_args.kwargs
    )


def test_create_target_session_includes_a_stripped_external_id():
    base_session, sts = _base_session_with_sts()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "a",
            "SecretAccessKey": "b",
            "SessionToken": "c",
        }
    }

    with patch(
        "src.auth.aws_session.boto3.Session"
    ):
        create_target_session(
            base_session=base_session,
            region_name="ap-northeast-2",
            role_arn="arn:aws:iam::826846563965:role/observer",
            external_id="  ext-123  ",
        )

    assert (
        sts.assume_role.call_args.kwargs[
            "ExternalId"
        ]
        == "ext-123"
    )


def test_create_target_session_returns_the_new_session():
    base_session, sts = _base_session_with_sts()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "a",
            "SecretAccessKey": "b",
            "SessionToken": "c",
        }
    }

    with patch(
        "src.auth.aws_session.boto3.Session"
    ) as session_cls:
        result = create_target_session(
            base_session=base_session,
            region_name="ap-northeast-2",
            role_arn="arn:aws:iam::826846563965:role/observer",
        )

    assert result is session_cls.return_value
    assert result is not base_session


# --- get_session_account_id -----------------------------------------------


def test_get_session_account_id_returns_the_account():
    session = MagicMock()
    sts = MagicMock()
    sts.get_caller_identity.return_value = {
        "Account": "826846563965"
    }
    session.client.return_value = sts

    assert (
        get_session_account_id(
            session=session,
            region_name="ap-northeast-2",
        )
        == "826846563965"
    )


def test_get_session_account_id_rejects_a_missing_account():
    session = MagicMock()
    sts = MagicMock()
    sts.get_caller_identity.return_value = {}
    session.client.return_value = sts

    with pytest.raises(ValueError):
        get_session_account_id(
            session=session,
            region_name="ap-northeast-2",
        )


def test_get_session_account_id_rejects_a_blank_account():
    session = MagicMock()
    sts = MagicMock()
    sts.get_caller_identity.return_value = {
        "Account": "   "
    }
    session.client.return_value = sts

    with pytest.raises(ValueError):
        get_session_account_id(
            session=session,
            region_name="ap-northeast-2",
        )
