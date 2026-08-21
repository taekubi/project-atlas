"""Tests for the pure/local-filesystem helpers in
src.handlers.cloudwatch_curated_handler (S3 calls mocked)."""

from unittest.mock import MagicMock

import pytest

from src.handlers.cloudwatch_curated_handler import (
    _download_raw_objects,
    _has_account_partition,
    _reset_local_dir,
    _required_env,
    _upload_curated_files,
)


# --- _required_env ------------------------------------------------------


def test_required_env_returns_a_present_value(monkeypatch):
    monkeypatch.setenv("ATLAS_BUCKET", "b")

    assert _required_env("ATLAS_BUCKET") == "b"


def test_required_env_rejects_a_missing_value(monkeypatch):
    monkeypatch.delenv("ATLAS_BUCKET", raising=False)

    with pytest.raises(ValueError):
        _required_env("ATLAS_BUCKET")


def test_required_env_rejects_a_blank_value(monkeypatch):
    monkeypatch.setenv("ATLAS_BUCKET", "   ")

    with pytest.raises(ValueError):
        _required_env("ATLAS_BUCKET")


# --- _reset_local_dir (real filesystem) -----------------------------


def test_reset_local_dir_creates_a_missing_directory(tmp_path):
    target = tmp_path / "scratch"

    _reset_local_dir(target)

    assert target.is_dir()


def test_reset_local_dir_clears_stale_contents(tmp_path):
    target = tmp_path / "scratch"
    target.mkdir()
    stale_file = target / "leftover.json"
    stale_file.write_text("{}")

    _reset_local_dir(target)

    assert target.is_dir()
    assert not stale_file.exists()
    assert list(target.iterdir()) == []


# --- _has_account_partition ---------------------------------------------


def test_has_account_partition_accepts_a_partitioned_key():
    assert _has_account_partition(
        "account_id=826846563965/region=ap-northeast-2/x.json"
    )


def test_has_account_partition_rejects_a_legacy_key():
    # Raw objects written before account_id partitioning was added
    # start directly with region= (or another segment) -- these are
    # the ones the curated transform must skip rather than crash on.
    assert not _has_account_partition(
        "region=ap-northeast-2/metric=CPUUtilization/x.json"
    )


def test_has_account_partition_rejects_a_bare_filename():
    assert not _has_account_partition("x.json")


# --- _download_raw_objects (mocked S3) -----------------------------------


def _s3_listing(pages):
    s3 = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = iter(pages)
    s3.get_paginator.return_value = paginator
    return s3


def test_download_raw_objects_downloads_partitioned_json_only(
    tmp_path,
):
    s3 = _s3_listing(
        [
            {
                "Contents": [
                    {
                        "Key": "raw/cloudwatch/account_id=826846563965/"
                        "region=ap-northeast-2/x.json"
                    }
                ]
            }
        ]
    )

    downloaded, skipped = _download_raw_objects(
        s3=s3,
        bucket_name="atlas-bucket",
        raw_prefix="raw/cloudwatch",
        local_root=tmp_path,
    )

    assert downloaded == 1
    assert skipped == 0
    s3.download_file.assert_called_once()


def test_download_raw_objects_skips_legacy_keys_without_downloading(
    tmp_path,
):
    s3 = _s3_listing(
        [
            {
                "Contents": [
                    {
                        "Key": "raw/cloudwatch/region=ap-northeast-2/"
                        "x.json"
                    }
                ]
            }
        ]
    )

    downloaded, skipped = _download_raw_objects(
        s3=s3,
        bucket_name="atlas-bucket",
        raw_prefix="raw/cloudwatch",
        local_root=tmp_path,
    )

    assert downloaded == 0
    assert skipped == 1
    s3.download_file.assert_not_called()


def test_download_raw_objects_ignores_non_json_keys(tmp_path):
    s3 = _s3_listing(
        [
            {
                "Contents": [
                    {
                        "Key": "raw/cloudwatch/account_id=826846563965/"
                        "_SUCCESS"
                    }
                ]
            }
        ]
    )

    downloaded, skipped = _download_raw_objects(
        s3=s3,
        bucket_name="atlas-bucket",
        raw_prefix="raw/cloudwatch",
        local_root=tmp_path,
    )

    assert downloaded == 0
    assert skipped == 0
    s3.download_file.assert_not_called()


def test_download_raw_objects_preserves_the_partition_layout_locally(
    tmp_path,
):
    s3 = _s3_listing(
        [
            {
                "Contents": [
                    {
                        "Key": "raw/cloudwatch/account_id=826846563965/"
                        "region=ap-northeast-2/x.json"
                    }
                ]
            }
        ]
    )

    _download_raw_objects(
        s3=s3,
        bucket_name="atlas-bucket",
        raw_prefix="raw/cloudwatch",
        local_root=tmp_path,
    )

    call = s3.download_file.call_args.kwargs
    expected_local = (
        tmp_path
        / "account_id=826846563965"
        / "region=ap-northeast-2"
        / "x.json"
    )
    assert call["Filename"] == str(
        expected_local
    )
    # The parent directory must exist before download_file writes into it.
    assert expected_local.parent.is_dir()


def test_download_raw_objects_strips_only_the_configured_prefix(
    tmp_path,
):
    # A leading/trailing slash on raw_prefix must not leak into the
    # relative key used to build the local path or count toward it.
    s3 = _s3_listing(
        [
            {
                "Contents": [
                    {
                        "Key": "raw/cloudwatch/account_id=1/x.json"
                    }
                ]
            }
        ]
    )

    _download_raw_objects(
        s3=s3,
        bucket_name="atlas-bucket",
        raw_prefix="/raw/cloudwatch/",
        local_root=tmp_path,
    )

    call = s3.download_file.call_args.kwargs
    assert call["Filename"] == str(
        tmp_path / "account_id=1" / "x.json"
    )


# --- _upload_curated_files (mocked S3) ------------------------------


def test_upload_curated_files_builds_keys_from_the_relative_path(
    tmp_path,
):
    s3 = MagicMock()
    output_root = tmp_path / "curated"
    file_path = (
        output_root
        / "account_id=826846563965"
        / "region=ap-northeast-2"
        / "date=2026-08-21"
        / "hour=06"
        / "metrics.parquet"
    )
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"parquet-bytes")

    uploaded_count = _upload_curated_files(
        s3=s3,
        bucket_name="atlas-bucket",
        curated_prefix="curated/cloudwatch",
        output_root=output_root,
        output_files=[file_path],
    )

    assert uploaded_count == 1
    s3.upload_file.assert_called_once_with(
        Filename=str(file_path),
        Bucket="atlas-bucket",
        Key=(
            "curated/cloudwatch/account_id=826846563965/"
            "region=ap-northeast-2/date=2026-08-21/"
            "hour=06/metrics.parquet"
        ),
    )


def test_upload_curated_files_returns_zero_for_an_empty_list(
    tmp_path,
):
    s3 = MagicMock()

    assert (
        _upload_curated_files(
            s3=s3,
            bucket_name="atlas-bucket",
            curated_prefix="curated/cloudwatch",
            output_root=tmp_path,
            output_files=[],
        )
        == 0
    )
    s3.upload_file.assert_not_called()
