"""Shared input validation for Project Atlas query builders.

Query inputs (account_id, region, date, resource_id) are validated
against strict patterns rather than passed through as-is, since they are
interpolated directly into SQL/CloudWatch dimension values and will
eventually be filled in from a Slack request (or AI-parsed free text)
rather than typed by hand.
"""

from __future__ import annotations

import re

ACCOUNT_ID_PATTERN = re.compile(r"^\d{12}$")
REGION_PATTERN = re.compile(r"^[a-z]{2}-[a-z]+-\d$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def validate(
    value: str,
    pattern: re.Pattern[str],
    label: str,
) -> str:
    """Validate a value against its expected shape."""

    if not pattern.match(value):
        raise ValueError(
            f"{label} is invalid: {value!r}"
        )

    return value


def validate_account_id(
    account_id: str,
) -> str:
    """Validate a 12-digit AWS account ID."""

    return validate(
        account_id,
        ACCOUNT_ID_PATTERN,
        "account_id",
    )


def validate_region(
    region: str,
) -> str:
    """Validate an AWS region name."""

    return validate(
        region,
        REGION_PATTERN,
        "region",
    )


def validate_date(
    date: str,
) -> str:
    """Validate a YYYY-MM-DD date string."""

    return validate(
        date,
        DATE_PATTERN,
        "date",
    )


def validate_resource_id(
    resource_id: str,
) -> str:
    """Validate an RDS/Aurora resource identifier."""

    return validate(
        resource_id,
        RESOURCE_ID_PATTERN,
        "resource_id",
    )
