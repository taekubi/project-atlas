"""Tests for src.storage.s3_uploader (no real AWS calls)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.storage.s3_uploader import (
    build_object_key,
    upload_file,
)


# --- build_object_key ----------------------------------------------------


def test_build_object_key_preserves_the_partition_path(tmp_path):
    source_root = tmp_path / "raw"
    file_path = (
        source_root
        / "account_id=826846563965"
        / "region=ap-northeast-2"
        / "metric=CPUUtilization"
        / "date=2026-08-21"
        / "watchcon-a_20260821T060000Z.json"
    )
    file_path.parent.mkdir(parents=True)
    file_path.write_text("{}")

    key = build_object_key(
        file_path=file_path,
        source_root=source_root,
        prefix="raw/cloudwatch",
    )

    assert key == (
        "raw/cloudwatch/account_id=826846563965/"
        "region=ap-northeast-2/metric=CPUUtilization/"
        "date=2026-08-21/"
        "watchcon-a_20260821T060000Z.json"
    )


def test_build_object_key_strips_slashes_from_the_prefix(tmp_path):
    source_root = tmp_path / "raw"
    file_path = source_root / "x.json"
    file_path.parent.mkdir(parents=True)
    file_path.write_text("{}")

    key = build_object_key(
        file_path=file_path,
        source_root=source_root,
        prefix="/raw/cloudwatch/",
    )

    assert key == "raw/cloudwatch/x.json"


def test_build_object_key_rejects_a_file_outside_the_source_root(
    tmp_path,
):
    source_root = tmp_path / "raw"
    source_root.mkdir()

    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    file_path = other_dir / "x.json"
    file_path.write_text("{}")

    with pytest.raises(
        ValueError, match="source root"
    ):
        build_object_key(
            file_path=file_path,
            source_root=source_root,
            prefix="raw/cloudwatch",
        )


def test_build_object_key_uses_forward_slashes_for_nested_paths(
    tmp_path,
):
    # Object keys must be POSIX-style regardless of the OS this runs
    # on, since a Windows-built backslash key would not match what S3
    # or Athena's Hive partitioning expects.
    source_root = tmp_path / "raw"
    file_path = (
        source_root / "a" / "b" / "c.json"
    )
    file_path.parent.mkdir(parents=True)
    file_path.write_text("{}")

    key = build_object_key(
        file_path=file_path,
        source_root=source_root,
        prefix="raw",
    )

    assert key == "raw/a/b/c.json"
    assert "\\" not in key


# --- upload_file -----------------------------------------------------


def test_upload_file_uses_the_given_profile():
    with patch(
        "src.storage.s3_uploader.boto3.Session"
    ) as session_cls:
        s3 = MagicMock()
        session_cls.return_value.client.return_value = s3
        s3.head_object.return_value = {
            "ContentLength": 123
        }

        upload_file(
            profile_name="atlas-test",
            region_name="ap-northeast-2",
            bucket_name="atlas-bucket",
            file_path=Path("x.json"),
            object_key="raw/x.json",
        )

    session_cls.assert_called_once_with(
        profile_name="atlas-test",
        region_name="ap-northeast-2",
    )


def test_upload_file_omits_profile_when_none():
    with patch(
        "src.storage.s3_uploader.boto3.Session"
    ) as session_cls:
        s3 = MagicMock()
        session_cls.return_value.client.return_value = s3
        s3.head_object.return_value = {}

        upload_file(
            profile_name=None,
            region_name="ap-northeast-2",
            bucket_name="atlas-bucket",
            file_path=Path("x.json"),
            object_key="raw/x.json",
        )

    session_cls.assert_called_once_with(
        region_name="ap-northeast-2",
    )


def test_upload_file_sets_json_content_type_and_verifies_the_object():
    with patch(
        "src.storage.s3_uploader.boto3.Session"
    ) as session_cls:
        s3 = MagicMock()
        session_cls.return_value.client.return_value = s3
        s3.head_object.return_value = {
            "ContentLength": 42
        }

        result = upload_file(
            profile_name="atlas-test",
            region_name="ap-northeast-2",
            bucket_name="atlas-bucket",
            file_path=Path("x.json"),
            object_key="raw/x.json",
        )

    s3.upload_file.assert_called_once_with(
        Filename="x.json",
        Bucket="atlas-bucket",
        Key="raw/x.json",
        ExtraArgs={
            "ContentType": "application/json"
        },
    )
    s3.head_object.assert_called_once_with(
        Bucket="atlas-bucket", Key="raw/x.json"
    )
    assert result == {"ContentLength": 42}
