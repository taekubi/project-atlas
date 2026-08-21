"""AWS Lambda handler to refresh the Curated CloudWatch Parquet layer.

Downloads Raw CloudWatch JSON from S3, runs the raw-to-curated transform,
uploads the resulting hourly Parquet partitions back to S3, and repairs
the Athena table so new partitions are queryable immediately.

At the current data scale (low thousands of Raw objects, a few MB) a full
reprocess of the whole Raw prefix on every run is cheap and simplest, and
each hourly partition is overwritten deterministically so reruns are safe.
This does not scale to the ~170k objects/month volume the small-files
problem was originally sized for; incremental windows/checkpoints are
still an open design question (see the KPI handoff doc, "Incremental
Curated processing") and should be revisited before that point.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.client import BaseClient

from src.observability.logger import (
    elapsed_ms,
    get_logger,
    request_id as context_request_id,
)
from src.query.athena_client import (
    create_session as create_athena_session,
    run_query,
)
from src.transforms.cloudwatch_curated import (
    transform_raw_to_curated,
)

logger = get_logger(__name__)

_LOCAL_INPUT_ROOT = Path("/tmp/atlas-curated-input")
_LOCAL_OUTPUT_ROOT = Path("/tmp/atlas-curated-output")


def _required_env(name: str) -> str:
    """Return a required environment variable."""

    value = os.getenv(name)

    if value is None or not value.strip():
        raise ValueError(
            f"Missing required environment variable: {name}"
        )

    return value.strip()


def _reset_local_dir(path: Path) -> None:
    """Remove and recreate a local scratch directory."""

    if path.exists():
        shutil.rmtree(path)

    path.mkdir(parents=True)


def _has_account_partition(
    relative_key: str,
) -> bool:
    """Return whether a Raw object key starts with a Hive account_id partition.

    Raw objects written before account_id partitioning was added lack this
    segment and cannot be flattened; they are intentionally excluded here,
    matching the existing curated-layer decision to skip legacy Raw data.
    """

    first_segment = relative_key.split(
        "/", 1
    )[0]

    return first_segment.startswith(
        "account_id="
    )


def _download_raw_objects(
    s3: BaseClient,
    bucket_name: str,
    raw_prefix: str,
    local_root: Path,
) -> tuple[int, int]:
    """Download partitioned Raw JSON objects to a local scratch directory."""

    clean_prefix = raw_prefix.strip("/")

    paginator = s3.get_paginator(
        "list_objects_v2"
    )

    downloaded_count = 0
    skipped_legacy_count = 0

    for page in paginator.paginate(
        Bucket=bucket_name,
        Prefix=f"{clean_prefix}/",
    ):
        for obj in page.get(
            "Contents", []
        ):
            key = obj["Key"]

            if not key.endswith(".json"):
                continue

            relative_key = key[
                len(clean_prefix) + 1:
            ]

            if not _has_account_partition(
                relative_key
            ):
                skipped_legacy_count += 1
                continue

            local_path = (
                local_root / relative_key
            )
            local_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            s3.download_file(
                Bucket=bucket_name,
                Key=key,
                Filename=str(local_path),
            )
            downloaded_count += 1

    return (
        downloaded_count,
        skipped_legacy_count,
    )


def _upload_curated_files(
    s3: BaseClient,
    bucket_name: str,
    curated_prefix: str,
    output_root: Path,
    output_files: list[Path],
) -> int:
    """Upload curated Parquet partitions to S3, preserving their layout."""

    clean_prefix = curated_prefix.strip(
        "/"
    )

    for path in output_files:
        relative_path = path.relative_to(
            output_root
        )

        key = (
            f"{clean_prefix}/"
            f"{relative_path.as_posix()}"
        )

        s3.upload_file(
            Filename=str(path),
            Bucket=bucket_name,
            Key=key,
        )

    return len(output_files)


def lambda_handler(
    event: dict[str, Any] | None,
    context: Any,
) -> dict[str, Any]:
    """Refresh the curated Parquet layer and repair the Athena table."""

    bucket_name = _required_env(
        "ATLAS_BUCKET"
    )
    storage_region = _required_env(
        "ATLAS_STORAGE_REGION"
    )
    athena_output_location = _required_env(
        "ATLAS_ATHENA_OUTPUT_LOCATION"
    )

    raw_prefix = os.getenv(
        "ATLAS_RAW_PREFIX",
        "raw/cloudwatch",
    ).strip()
    curated_prefix = os.getenv(
        "ATLAS_CURATED_PREFIX",
        "curated/cloudwatch",
    ).strip()
    athena_database = os.getenv(
        "ATLAS_ATHENA_DATABASE",
        "project_atlas",
    ).strip()
    athena_table = os.getenv(
        "ATLAS_ATHENA_TABLE",
        "cloudwatch_metrics",
    ).strip()
    athena_workgroup = os.getenv(
        "ATLAS_ATHENA_WORKGROUP",
        "primary",
    ).strip()

    started = time.perf_counter()

    logger.info(
        "curated_refresh_started",
        extra={
            "request_id": (
                context_request_id(context)
            ),
            "bucket": bucket_name,
            "raw_prefix": raw_prefix,
            "curated_prefix": curated_prefix,
            "athena_table": (
                f"{athena_database}."
                f"{athena_table}"
            ),
        },
    )

    _reset_local_dir(_LOCAL_INPUT_ROOT)
    _reset_local_dir(_LOCAL_OUTPUT_ROOT)

    session = boto3.Session(
        region_name=storage_region,
    )
    s3 = session.client("s3")

    (
        downloaded_count,
        skipped_legacy_count,
    ) = _download_raw_objects(
        s3=s3,
        bucket_name=bucket_name,
        raw_prefix=raw_prefix,
        local_root=_LOCAL_INPUT_ROOT,
    )

    result = transform_raw_to_curated(
        input_root=_LOCAL_INPUT_ROOT,
        output_root=_LOCAL_OUTPUT_ROOT,
    )

    uploaded_count = _upload_curated_files(
        s3=s3,
        bucket_name=bucket_name,
        curated_prefix=curated_prefix,
        output_root=_LOCAL_OUTPUT_ROOT,
        output_files=result[
            "output_files"
        ],
    )

    athena_session = create_athena_session(
        profile_name=None,
        region_name=storage_region,
    )

    run_query(
        session=athena_session,
        database=athena_database,
        output_location=(
            athena_output_location
        ),
        query=(
            "MSCK REPAIR TABLE "
            f"{athena_database}."
            f"{athena_table}"
        ),
        workgroup=athena_workgroup,
    )

    request_id = (
        getattr(
            context,
            "aws_request_id",
            None,
        )
        if context is not None
        else None
    )

    logger.info(
        "curated_refresh_succeeded",
        extra={
            "request_id": request_id,
            "downloaded_raw_file_count": (
                downloaded_count
            ),
            "skipped_legacy_file_count": (
                skipped_legacy_count
            ),
            "curated_row_count": result[
                "curated_row_count"
            ],
            "duplicates_removed": result[
                "duplicates_removed"
            ],
            "parquet_file_count": result[
                "parquet_file_count"
            ],
            "uploaded_curated_file_count": (
                uploaded_count
            ),
            "duration_ms": elapsed_ms(started),
        },
    )

    return {
        "status": "success",
        "request_id": request_id,
        "downloaded_raw_file_count": (
            downloaded_count
        ),
        "skipped_legacy_file_count": (
            skipped_legacy_count
        ),
        "raw_file_count": result[
            "raw_file_count"
        ],
        "no_data_file_count": result[
            "no_data_file_count"
        ],
        "curated_row_count": result[
            "curated_row_count"
        ],
        "duplicates_removed": result[
            "duplicates_removed"
        ],
        "parquet_file_count": result[
            "parquet_file_count"
        ],
        "parquet_total_bytes": result[
            "parquet_total_bytes"
        ],
        "uploaded_curated_file_count": (
            uploaded_count
        ),
    }
