"""Upload Project Atlas raw data files to Amazon S3."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import boto3
from boto3.s3.transfer import S3UploadFailedError
from botocore.exceptions import BotoCoreError, ClientError, ProfileNotFound


def build_object_key(
    file_path: Path,
    source_root: Path,
    prefix: str,
) -> str:
    """Build an S3 object key while preserving the local partition path."""

    resolved_file = file_path.resolve()
    resolved_root = source_root.resolve()

    try:
        relative_path = resolved_file.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError(
            f"File must be located under source root: {source_root}"
        ) from error

    clean_prefix = prefix.strip("/")

    return f"{clean_prefix}/{relative_path.as_posix()}"


def upload_file(
    profile_name: str,
    region_name: str,
    bucket_name: str,
    file_path: Path,
    object_key: str,
) -> dict[str, Any]:
    """Upload a local JSON file and verify the resulting S3 object."""

    session = boto3.Session(
        profile_name=profile_name,
        region_name=region_name,
    )

    s3 = session.client("s3")

    s3.upload_file(
        Filename=str(file_path),
        Bucket=bucket_name,
        Key=object_key,
        ExtraArgs={
            "ContentType": "application/json",
        },
    )

    return s3.head_object(
        Bucket=bucket_name,
        Key=object_key,
    )


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description="Upload Project Atlas raw data to Amazon S3."
    )

    parser.add_argument(
        "--file",
        required=True,
        help="Local file to upload",
    )
    parser.add_argument(
        "--bucket",
        required=True,
        help="Destination S3 bucket",
    )
    parser.add_argument(
        "--profile",
        default="atlas-test",
        help="AWS CLI profile",
    )
    parser.add_argument(
        "--region",
        default="ap-northeast-2",
        help="AWS Region",
    )
    parser.add_argument(
        "--source-root",
        default="data/raw/cloudwatch",
        help="Local source directory used to build the relative key",
    )
    parser.add_argument(
        "--prefix",
        default="raw/cloudwatch",
        help="Destination S3 key prefix",
    )

    return parser.parse_args()


def main() -> None:
    """Run the S3 raw data uploader."""

    args = parse_arguments()

    file_path = Path(args.file)
    source_root = Path(args.source_root)

    if not file_path.is_file():
        raise SystemExit(f"File does not exist: {file_path}")

    try:
        object_key = build_object_key(
            file_path=file_path,
            source_root=source_root,
            prefix=args.prefix,
        )

        metadata = upload_file(
            profile_name=args.profile,
            region_name=args.region,
            bucket_name=args.bucket,
            file_path=file_path,
            object_key=object_key,
        )

        print(f"Uploaded: s3://{args.bucket}/{object_key}")
        print(f"Size: {metadata['ContentLength']} bytes")
        print(f"Content type: {metadata.get('ContentType')}")
        print(f"Encryption: {metadata.get('ServerSideEncryption')}")

    except ProfileNotFound as error:
        raise SystemExit(f"AWS profile not found: {error}") from error
    except ValueError as error:
        raise SystemExit(str(error)) from error
    except (
        ClientError,
        BotoCoreError,
        S3UploadFailedError,
    ) as error:
        raise SystemExit(f"S3 upload failed: {error}") from error


if __name__ == "__main__":
    main()