"""Tests for the pure/config helpers in
src.handlers.configured_rds_metrics_handler (S3 calls mocked)."""

from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from src.config.atlas_config import (
    AtlasConfig,
    AtlasSettings,
    CollectionSettings,
    TargetSettings,
)
from src.handlers.configured_rds_metrics_handler import (
    _apply_runtime_overrides,
    _download_config,
    _required_env,
)


def _config(
    bucket="atlas-bucket",
    storage_region="ap-northeast-2",
) -> AtlasConfig:
    return AtlasConfig(
        atlas=AtlasSettings(
            storage_region=storage_region,
            bucket=bucket,
            s3_prefix="raw/cloudwatch",
            source_root="tmp/rds-discovery-metrics",
        ),
        collection=CollectionSettings(
            lookback_minutes=15,
            period_seconds=300,
            metric_profile="operational-v1",
            metrics=[],
        ),
        targets=[
            TargetSettings(
                name="headquarters",
                account_id="826846563965",
                role_name="observer",
                regions=["ap-northeast-2"],
                enabled=True,
            )
        ],
    )


# --- _required_env ------------------------------------------------------


def test_required_env_returns_a_present_value(monkeypatch):
    monkeypatch.setenv("ATLAS_BUCKET", "b")

    assert _required_env("ATLAS_BUCKET") == "b"


def test_required_env_rejects_a_missing_value(monkeypatch):
    monkeypatch.delenv("ATLAS_BUCKET", raising=False)

    with pytest.raises(ValueError):
        _required_env("ATLAS_BUCKET")


# --- _apply_runtime_overrides ---------------------------------------


def test_apply_runtime_overrides_uses_the_default_output_root(
    monkeypatch,
):
    monkeypatch.delenv("ATLAS_OUTPUT_ROOT", raising=False)

    result = _apply_runtime_overrides(
        _config()
    )

    assert (
        result.atlas.source_root
        == "/tmp/rds-discovery-metrics"
    )


def test_apply_runtime_overrides_uses_a_configured_output_root(
    monkeypatch,
):
    monkeypatch.setenv(
        "ATLAS_OUTPUT_ROOT", "/tmp/custom-root"
    )

    result = _apply_runtime_overrides(
        _config()
    )

    assert (
        result.atlas.source_root
        == "/tmp/custom-root"
    )


def test_apply_runtime_overrides_rejects_a_blank_output_root(
    monkeypatch,
):
    monkeypatch.setenv("ATLAS_OUTPUT_ROOT", "   ")

    with pytest.raises(
        ValueError, match="ATLAS_OUTPUT_ROOT"
    ):
        _apply_runtime_overrides(_config())


def test_apply_runtime_overrides_leaves_other_config_fields_untouched(
    monkeypatch,
):
    monkeypatch.delenv("ATLAS_OUTPUT_ROOT", raising=False)
    original = _config()

    result = _apply_runtime_overrides(original)

    assert result.atlas.bucket == original.atlas.bucket
    assert (
        result.collection
        == original.collection
    )
    assert result.targets == original.targets


# --- _download_config (S3 mocked) ------------------------------------


def test_download_config_returns_the_loaded_config(tmp_path):
    with patch(
        "src.handlers.configured_rds_metrics_handler.boto3.Session"
    ) as session_cls, patch(
        "src.handlers.configured_rds_metrics_handler.load_config",
        return_value=_config(),
    ) as mocked_load:
        s3 = MagicMock()
        session_cls.return_value.client.return_value = s3

        result = _download_config(
            bucket_name="atlas-bucket",
            object_key="config/atlas.toml",
            storage_region="ap-northeast-2",
            local_path=tmp_path / "atlas.toml",
        )

    s3.download_file.assert_called_once_with(
        Bucket="atlas-bucket",
        Key="config/atlas.toml",
        Filename=str(tmp_path / "atlas.toml"),
    )
    mocked_load.assert_called_once()
    assert result.atlas.bucket == "atlas-bucket"


def test_download_config_creates_the_local_parent_directory(tmp_path):
    local_path = (
        tmp_path / "nested" / "atlas.toml"
    )

    with patch(
        "src.handlers.configured_rds_metrics_handler.boto3.Session"
    ) as session_cls, patch(
        "src.handlers.configured_rds_metrics_handler.load_config",
        return_value=_config(),
    ):
        session_cls.return_value.client.return_value = (
            MagicMock()
        )

        _download_config(
            bucket_name="atlas-bucket",
            object_key="config/atlas.toml",
            storage_region="ap-northeast-2",
            local_path=local_path,
        )

    assert local_path.parent.is_dir()


def test_download_config_rejects_a_bucket_mismatch(tmp_path):
    # The Lambda's ATLAS_BUCKET env var and the config file's own
    # atlas.bucket must agree, or metrics could silently be written to
    # (or read from) the wrong S3 bucket.
    with patch(
        "src.handlers.configured_rds_metrics_handler.boto3.Session"
    ) as session_cls, patch(
        "src.handlers.configured_rds_metrics_handler.load_config",
        return_value=_config(bucket="other-bucket"),
    ):
        session_cls.return_value.client.return_value = (
            MagicMock()
        )

        with pytest.raises(
            ValueError, match="bucket"
        ):
            _download_config(
                bucket_name="atlas-bucket",
                object_key="config/atlas.toml",
                storage_region="ap-northeast-2",
                local_path=tmp_path / "atlas.toml",
            )


def test_download_config_rejects_a_region_mismatch(tmp_path):
    with patch(
        "src.handlers.configured_rds_metrics_handler.boto3.Session"
    ) as session_cls, patch(
        "src.handlers.configured_rds_metrics_handler.load_config",
        return_value=_config(
            storage_region="us-east-1"
        ),
    ):
        session_cls.return_value.client.return_value = (
            MagicMock()
        )

        with pytest.raises(
            ValueError, match="Region"
        ):
            _download_config(
                bucket_name="atlas-bucket",
                object_key="config/atlas.toml",
                storage_region="ap-northeast-2",
                local_path=tmp_path / "atlas.toml",
            )
