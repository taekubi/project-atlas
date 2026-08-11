"""Create AWS sessions for local, Lambda, and cross-account execution."""

from __future__ import annotations

from typing import Any

import boto3


def create_target_session(
    base_session: boto3.Session,
    region_name: str,
    role_arn: str | None = None,
    role_session_name: str = "project-atlas-cloudwatch",
    external_id: str | None = None,
) -> boto3.Session:
    """Return the base session or a cross-account assumed-role session."""

    if not role_arn:
        return base_session

    clean_role_arn = role_arn.strip()
    clean_session_name = role_session_name.strip()

    if not clean_role_arn:
        return base_session

    if not clean_session_name:
        raise ValueError(
            "role_session_name must not be empty"
        )

    sts = base_session.client(
        "sts",
        region_name=region_name,
    )

    request: dict[str, Any] = {
        "RoleArn": clean_role_arn,
        "RoleSessionName": clean_session_name,
    }

    if external_id and external_id.strip():
        request["ExternalId"] = external_id.strip()

    response = sts.assume_role(**request)
    credentials = response["Credentials"]

    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region_name,
    )


def get_session_account_id(
    session: boto3.Session,
    region_name: str,
) -> str:
    """Return the AWS account ID represented by a Boto3 session."""

    sts = session.client(
        "sts",
        region_name=region_name,
    )

    response = sts.get_caller_identity()
    account_id = str(response.get("Account", "")).strip()

    if not account_id:
        raise ValueError(
            "Unable to determine AWS account ID from the session"
        )

    return account_id