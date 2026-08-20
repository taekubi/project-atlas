"""Tests for src.query.validators."""

import pytest

from src.query.validators import (
    validate_account_id,
    validate_date,
    validate_region,
    validate_resource_id,
)


def test_validate_account_id_accepts_12_digits():
    assert validate_account_id("826846563965") == "826846563965"


@pytest.mark.parametrize(
    "value",
    ["12345", "abcdefghijkl", "826846563965 ", "826-846-563-965"],
)
def test_validate_account_id_rejects_invalid(value):
    with pytest.raises(ValueError):
        validate_account_id(value)


def test_validate_region_accepts_valid_region():
    assert validate_region("ap-northeast-2") == "ap-northeast-2"


@pytest.mark.parametrize(
    "value",
    ["us-east", "AP-NORTHEAST-2", "ap_northeast_2", ""],
)
def test_validate_region_rejects_invalid(value):
    with pytest.raises(ValueError):
        validate_region(value)


def test_validate_date_accepts_iso_date():
    assert validate_date("2026-08-19") == "2026-08-19"


@pytest.mark.parametrize(
    "value",
    ["2026-8-19", "20260819", "2026/08/19", "not-a-date"],
)
def test_validate_date_rejects_invalid(value):
    with pytest.raises(ValueError):
        validate_date(value)


def test_validate_resource_id_accepts_hyphens_and_underscores():
    assert validate_resource_id("watchcon-a_1") == "watchcon-a_1"


@pytest.mark.parametrize(
    "value",
    ["watchcon;drop table", "watchcon a", "watchcon'--"],
)
def test_validate_resource_id_rejects_invalid(value):
    with pytest.raises(ValueError):
        validate_resource_id(value)
