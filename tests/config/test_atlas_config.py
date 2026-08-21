"""Tests for src.config.atlas_config (real TOML files via tmp_path)."""

import pytest

from src.config.atlas_config import (
    _optional_string,
    _optional_string_list,
    _positive_int,
    _required_string,
    _string_list,
    load_config,
)

_VALID_TOML = """
[atlas]
storage_region = "ap-northeast-2"
bucket = "project-atlas-data-714933490352-ap-northeast-2"
s3_prefix = "raw/cloudwatch"
source_root = "tmp/rds-discovery-metrics"

[collection]
lookback_minutes = 15
period_seconds = 300
metric_profile = "operational-v1"

[[targets]]
name = "headquarters"
account_id = "826846563965"
role_name = "project-atlas-rds-observer-role"
regions = ["ap-northeast-2"]
enabled = true

[[targets]]
name = "disabled-target"
account_id = "111111111111"
role_name = "project-atlas-rds-observer-role"
regions = ["ap-northeast-2"]
enabled = false
"""


def _write(tmp_path, text: str):
    path = tmp_path / "atlas.toml"
    path.write_text(text, encoding="utf-8")
    return path


# --- _required_string ---------------------------------------------------


def test_required_string_strips_whitespace():
    assert (
        _required_string(
            {"k": "  value  "}, "k", "s"
        )
        == "value"
    )


def test_required_string_rejects_a_missing_key():
    with pytest.raises(ValueError, match="s.k"):
        _required_string({}, "k", "s")


def test_required_string_rejects_a_non_string():
    with pytest.raises(ValueError):
        _required_string({"k": 5}, "k", "s")


def test_required_string_rejects_a_blank_string():
    with pytest.raises(ValueError):
        _required_string({"k": "   "}, "k", "s")


# --- _optional_string ----------------------------------------------------


def test_optional_string_returns_none_when_absent():
    assert (
        _optional_string({}, "k", "s") is None
    )


def test_optional_string_strips_a_present_value():
    assert (
        _optional_string(
            {"k": " v "}, "k", "s"
        )
        == "v"
    )


def test_optional_string_rejects_a_blank_present_value():
    with pytest.raises(ValueError):
        _optional_string(
            {"k": "  "}, "k", "s"
        )


# --- _positive_int ---------------------------------------------------


def test_positive_int_accepts_a_positive_value():
    assert (
        _positive_int({"k": 15}, "k", "s")
        == 15
    )


@pytest.mark.parametrize("value", [0, -1, 1.5, "15", None])
def test_positive_int_rejects_non_positive_or_wrong_type(value):
    with pytest.raises(ValueError):
        _positive_int({"k": value}, "k", "s")


def test_positive_int_rejects_a_bool():
    # bool is a subclass of int in Python; True/False must not slip
    # through a "positive int" check as 1/0.
    with pytest.raises(ValueError):
        _positive_int({"k": True}, "k", "s")


# --- _string_list ---------------------------------------------------


def test_string_list_strips_each_entry():
    assert _string_list(
        {"k": [" a ", "b"]}, "k", "s"
    ) == ["a", "b"]


def test_string_list_rejects_an_empty_list():
    with pytest.raises(ValueError):
        _string_list({"k": []}, "k", "s")


def test_string_list_rejects_a_non_list():
    with pytest.raises(ValueError):
        _string_list(
            {"k": "ap-northeast-2"}, "k", "s"
        )


def test_string_list_rejects_a_blank_entry():
    with pytest.raises(ValueError):
        _string_list(
            {"k": ["ap-northeast-2", "  "]},
            "k",
            "s",
        )


# --- _optional_string_list ---------------------------------------------


def test_optional_string_list_returns_empty_when_absent():
    assert _optional_string_list(
        {}, "k", "s"
    ) == []


def test_optional_string_list_validates_a_present_value():
    with pytest.raises(ValueError):
        _optional_string_list(
            {"k": []}, "k", "s"
        )


# --- load_config: file-level errors -------------------------------------


def test_load_config_raises_on_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


def test_load_config_requires_an_atlas_section(tmp_path):
    path = _write(
        tmp_path,
        "[collection]\nlookback_minutes = 15\n"
        "period_seconds = 300\nmetrics = [\"CPUUtilization\"]\n",
    )

    with pytest.raises(ValueError, match=r"\[atlas\]"):
        load_config(path)


def test_load_config_requires_a_collection_section(tmp_path):
    path = _write(
        tmp_path,
        '[atlas]\nstorage_region = "ap-northeast-2"\n'
        'bucket = "b"\ns3_prefix = "p"\nsource_root = "r"\n',
    )

    with pytest.raises(
        ValueError, match=r"\[collection\]"
    ):
        load_config(path)


def test_load_config_requires_at_least_one_target(tmp_path):
    path = _write(
        tmp_path,
        _VALID_TOML.split("[[targets]]")[0],
    )

    with pytest.raises(
        ValueError, match="targets"
    ):
        load_config(path)


# --- load_config: collection metric selection ---------------------------


def test_load_config_rejects_neither_metric_profile_nor_metrics(
    tmp_path,
):
    text = _VALID_TOML.replace(
        'metric_profile = "operational-v1"',
        "",
    )
    path = _write(tmp_path, text)

    with pytest.raises(
        ValueError,
        match="metric_profile.*metrics|metrics.*metric_profile",
    ):
        load_config(path)


def test_load_config_rejects_both_metric_profile_and_metrics(
    tmp_path,
):
    text = _VALID_TOML.replace(
        'metric_profile = "operational-v1"',
        'metric_profile = "operational-v1"\n'
        'metrics = ["CPUUtilization"]',
    )
    path = _write(tmp_path, text)

    with pytest.raises(ValueError):
        load_config(path)


def test_load_config_accepts_an_explicit_metrics_list_instead_of_a_profile(
    tmp_path,
):
    text = _VALID_TOML.replace(
        'metric_profile = "operational-v1"',
        'metrics = ["CPUUtilization", "DatabaseConnections"]',
    )
    path = _write(tmp_path, text)

    config = load_config(path)

    assert config.collection.metric_profile is None
    assert config.collection.metrics == [
        "CPUUtilization",
        "DatabaseConnections",
    ]
    assert (
        config.collection.uses_metric_profile
        is False
    )


# --- load_config: target validation -------------------------------------


def test_load_config_rejects_a_non_12_digit_account_id(tmp_path):
    text = _VALID_TOML.replace(
        '"826846563965"', '"12345"'
    )
    path = _write(tmp_path, text)

    with pytest.raises(
        ValueError, match="account_id"
    ):
        load_config(path)


def test_load_config_rejects_a_non_numeric_account_id(tmp_path):
    text = _VALID_TOML.replace(
        '"826846563965"',
        '"82684656396a"',
    )
    path = _write(tmp_path, text)

    with pytest.raises(ValueError):
        load_config(path)


def test_load_config_rejects_a_non_boolean_enabled(tmp_path):
    text = _VALID_TOML.replace(
        "enabled = true", 'enabled = "true"'
    )
    path = _write(tmp_path, text)

    with pytest.raises(
        ValueError, match="enabled"
    ):
        load_config(path)


def test_load_config_defaults_enabled_to_true_when_omitted(tmp_path):
    text = _VALID_TOML.replace(
        "\nenabled = true", ""
    )
    path = _write(tmp_path, text)

    config = load_config(path)

    assert config.targets[0].enabled is True


def test_load_config_rejects_a_target_missing_regions(tmp_path):
    text = _VALID_TOML.replace(
        'regions = ["ap-northeast-2"]\nenabled = true',
        "enabled = true",
        1,
    )
    path = _write(tmp_path, text)

    with pytest.raises(
        ValueError, match="regions"
    ):
        load_config(path)


# --- load_config: full successful parse ----------------------------------


def test_load_config_parses_a_complete_valid_file(tmp_path):
    path = _write(tmp_path, _VALID_TOML)

    config = load_config(path)

    assert config.atlas.storage_region == (
        "ap-northeast-2"
    )
    assert config.atlas.bucket == (
        "project-atlas-data-714933490352-ap-northeast-2"
    )
    assert config.collection.lookback_minutes == 15
    assert config.collection.period_seconds == 300
    assert (
        config.collection.uses_metric_profile
        is True
    )
    assert len(config.targets) == 2


def test_load_config_target_role_arn_is_built_from_account_and_role_name(
    tmp_path,
):
    path = _write(tmp_path, _VALID_TOML)
    config = load_config(path)

    target = config.targets[0]

    assert target.role_arn == (
        "arn:aws:iam::826846563965:role/"
        "project-atlas-rds-observer-role"
    )


def test_load_config_enabled_targets_excludes_disabled_ones(tmp_path):
    path = _write(tmp_path, _VALID_TOML)
    config = load_config(path)

    assert [
        t.name for t in config.enabled_targets
    ] == ["headquarters"]
    assert len(config.targets) == 2
