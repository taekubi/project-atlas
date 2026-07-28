"""AWS Lambda handler for the Project Atlas CloudWatch-to-S3 pipeline."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from src.collectors.cloudwatch_metrics import METRIC_CONFIG
from src.pipelines.cloudwatch_s3_pipeline import run_pipeline


def _required_env(name: str) -> str:
    """Return a required environment variable."""

    value = os.getenv(name)

    if not value:
        raise ValueError(
            f"Missing required environment variable: {name}"
        )

    return value


def _positive_int_env(
    name: str,
    default: int,
) -> int:
    """Return a positive integer environment variable."""

    raw_value = os.getenv(name, str(default))

    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(
            f"{name} must be an integer: {raw_value}"
        ) from error

    if value <= 0:
        raise ValueError(
            f"{name} must be greater than 0: {value}"
        )

    return value


def _metric_names() -> list[str]:
    """Return validated CloudWatch metric names."""

    raw_metrics = os.getenv(
        "METRICS",
        ",".join(METRIC_CONFIG),
    )

    metric_names = [
        metric.strip()
        for metric in raw_metrics.split(",")
        if metric.strip()
    ]

    if not metric_names:
        raise ValueError(
            "METRICS must contain at least one metric"
        )

    unsupported_metrics = [
        metric
        for metric in metric_names
        if metric not in METRIC_CONFIG
    ]

    if unsupported_metrics:
        raise ValueError(
            "Unsupported CloudWatch metrics: "
            + ", ".join(unsupported_metrics)
        )

    return metric_names


def lambda_handler(
    event: dict[str, Any] | None,
    context: Any,
) -> dict[str, Any]:
    """Collect RDS metrics and upload JSON files to Amazon S3."""

    region_name = os.getenv(
        "AWS_REGION",
        "ap-northeast-2",
    )

    bucket_name = _required_env("ATLAS_BUCKET")
    db_instance_identifier = _required_env(
        "DB_INSTANCE_IDENTIFIER"
    )

    results = run_pipeline(
        profile_name=None,
        region_name=region_name,
        bucket_name=bucket_name,
        db_instance_identifier=db_instance_identifier,
        metric_names=_metric_names(),
        lookback_minutes=_positive_int_env(
            "LOOKBACK_MINUTES",
            60,
        ),
        period_seconds=_positive_int_env(
            "PERIOD_SECONDS",
            300,
        ),
        source_root=Path(
            os.getenv(
                "OUTPUT_ROOT",
                "/tmp/cloudwatch",
            )
        ),
        prefix=os.getenv(
            "S3_PREFIX",
            "raw/cloudwatch",
        ),
    )

    request_id = (
        getattr(context, "aws_request_id", None)
        if context is not None
        else None
    )

    return {
        "status": "success",
        "request_id": request_id,
        "region": region_name,
        "bucket": bucket_name,
        "db_instance_identifier": db_instance_identifier,
        "uploaded_count": len(results),
        "results": results,
    }