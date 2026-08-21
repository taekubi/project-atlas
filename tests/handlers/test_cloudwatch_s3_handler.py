"""Tests for the env-var parsing/validation helpers in
src.handlers.cloudwatch_s3_handler (no AWS calls)."""

import pytest

from src.handlers.cloudwatch_s3_handler import (
    _metric_names,
    _optional_env,
    _positive_int_env,
    _required_env,
    _resource_config,
    _role_config,
)


# --- _required_env / _optional_env ---------------------------------------


def test_required_env_returns_a_present_value(monkeypatch):
    monkeypatch.setenv("ATLAS_BUCKET", "b")

    assert _required_env("ATLAS_BUCKET") == "b"


def test_required_env_rejects_a_missing_value(monkeypatch):
    monkeypatch.delenv("ATLAS_BUCKET", raising=False)

    with pytest.raises(
        ValueError, match="ATLAS_BUCKET"
    ):
        _required_env("ATLAS_BUCKET")


def test_optional_env_returns_none_when_absent(monkeypatch):
    monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)

    assert _optional_env("TARGET_ROLE_ARN") is None


def test_optional_env_strips_and_treats_blank_as_none(monkeypatch):
    monkeypatch.setenv("TARGET_ROLE_ARN", "   ")

    assert _optional_env("TARGET_ROLE_ARN") is None


def test_optional_env_strips_a_present_value(monkeypatch):
    monkeypatch.setenv("TARGET_ROLE_ARN", " arn:aws:iam::x:role/y ")

    assert (
        _optional_env("TARGET_ROLE_ARN")
        == "arn:aws:iam::x:role/y"
    )


# --- _positive_int_env ----------------------------------------------------


def test_positive_int_env_uses_the_default_when_absent(monkeypatch):
    monkeypatch.delenv("LOOKBACK_MINUTES", raising=False)

    assert (
        _positive_int_env(
            "LOOKBACK_MINUTES", 60
        )
        == 60
    )


def test_positive_int_env_parses_a_present_value(monkeypatch):
    monkeypatch.setenv("LOOKBACK_MINUTES", "15")

    assert (
        _positive_int_env(
            "LOOKBACK_MINUTES", 60
        )
        == 15
    )


def test_positive_int_env_rejects_a_non_integer(monkeypatch):
    monkeypatch.setenv("LOOKBACK_MINUTES", "soon")

    with pytest.raises(
        ValueError, match="LOOKBACK_MINUTES"
    ):
        _positive_int_env(
            "LOOKBACK_MINUTES", 60
        )


def test_positive_int_env_rejects_zero(monkeypatch):
    monkeypatch.setenv("LOOKBACK_MINUTES", "0")

    with pytest.raises(ValueError):
        _positive_int_env(
            "LOOKBACK_MINUTES", 60
        )


def test_positive_int_env_rejects_a_negative_value(monkeypatch):
    monkeypatch.setenv("LOOKBACK_MINUTES", "-5")

    with pytest.raises(ValueError):
        _positive_int_env(
            "LOOKBACK_MINUTES", 60
        )


# --- _metric_names ---------------------------------------------------


def test_metric_names_defaults_to_every_known_metric(monkeypatch):
    monkeypatch.delenv("METRICS", raising=False)

    from src.collectors.cloudwatch_metrics import (
        METRIC_CONFIG,
    )

    assert _metric_names() == list(METRIC_CONFIG)


def test_metric_names_parses_a_comma_separated_list(monkeypatch):
    monkeypatch.setenv(
        "METRICS",
        "CPUUtilization, DatabaseConnections",
    )

    assert _metric_names() == [
        "CPUUtilization",
        "DatabaseConnections",
    ]


def test_metric_names_rejects_an_unsupported_metric(monkeypatch):
    monkeypatch.setenv("METRICS", "NotARealMetric")

    with pytest.raises(
        ValueError, match="NotARealMetric"
    ):
        _metric_names()


def test_metric_names_rejects_an_empty_list(monkeypatch):
    monkeypatch.setenv("METRICS", "  ,  ,")

    with pytest.raises(ValueError):
        _metric_names()


# --- _resource_config ----------------------------------------------------


def test_resource_config_defaults_to_db_instance_identifier(
    monkeypatch,
):
    monkeypatch.delenv(
        "RESOURCE_DIMENSION", raising=False
    )
    monkeypatch.setenv(
        "RESOURCE_ID", "watchcon-a"
    )

    dimension, resource_id = _resource_config()

    assert dimension == "DBInstanceIdentifier"
    assert resource_id == "watchcon-a"


def test_resource_config_rejects_an_unsupported_dimension(
    monkeypatch,
):
    monkeypatch.setenv(
        "RESOURCE_DIMENSION", "NotADimension"
    )
    monkeypatch.setenv(
        "RESOURCE_ID", "watchcon-a"
    )

    with pytest.raises(
        ValueError, match="dimension"
    ):
        _resource_config()


def test_resource_config_falls_back_to_the_legacy_env_var_name(
    monkeypatch,
):
    monkeypatch.delenv("RESOURCE_ID", raising=False)
    monkeypatch.setenv(
        "DB_INSTANCE_IDENTIFIER", "watchcon-a"
    )

    _, resource_id = _resource_config()

    assert resource_id == "watchcon-a"


def test_resource_config_rejects_a_missing_resource_id(monkeypatch):
    monkeypatch.delenv("RESOURCE_ID", raising=False)
    monkeypatch.delenv(
        "DB_INSTANCE_IDENTIFIER", raising=False
    )

    with pytest.raises(
        ValueError, match="RESOURCE_ID"
    ):
        _resource_config()


# --- _role_config -----------------------------------------------------


def test_role_config_defaults_to_no_cross_account_role(monkeypatch):
    monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)
    monkeypatch.delenv(
        "TARGET_EXTERNAL_ID", raising=False
    )
    monkeypatch.delenv(
        "ROLE_SESSION_NAME", raising=False
    )

    role_arn, external_id, session_name = (
        _role_config()
    )

    assert role_arn is None
    assert external_id is None
    assert session_name == "project-atlas-cloudwatch"


def test_role_config_rejects_an_external_id_without_a_role_arn(
    monkeypatch,
):
    monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)
    monkeypatch.setenv(
        "TARGET_EXTERNAL_ID", "ext-123"
    )

    with pytest.raises(
        ValueError,
        match="TARGET_EXTERNAL_ID",
    ):
        _role_config()


def test_role_config_accepts_a_role_arn_with_an_external_id(
    monkeypatch,
):
    monkeypatch.setenv(
        "TARGET_ROLE_ARN",
        "arn:aws:iam::826846563965:role/observer",
    )
    monkeypatch.setenv(
        "TARGET_EXTERNAL_ID", "ext-123"
    )

    role_arn, external_id, _ = _role_config()

    assert role_arn == (
        "arn:aws:iam::826846563965:role/observer"
    )
    assert external_id == "ext-123"


def test_role_config_rejects_a_blank_session_name(monkeypatch):
    monkeypatch.delenv("TARGET_ROLE_ARN", raising=False)
    monkeypatch.delenv(
        "TARGET_EXTERNAL_ID", raising=False
    )
    monkeypatch.setenv("ROLE_SESSION_NAME", "   ")

    with pytest.raises(
        ValueError, match="ROLE_SESSION_NAME"
    ):
        _role_config()
