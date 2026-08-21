"""AWS Lambda handler for configured Project Atlas collection."""

from __future__ import annotations

from dataclasses import replace
import os
import time
from pathlib import Path
from typing import Any

import boto3

from src.config.atlas_config import (
    AtlasConfig,
    load_config,
)
from src.observability.logger import (
    elapsed_ms,
    get_logger,
    request_id as context_request_id,
)
from src.pipelines.configured_rds_metrics_pipeline import (
    run_configured_pipeline,
)

logger = get_logger(__name__)


def _required_env(name: str) -> str:
    """Return a required environment variable."""

    value = os.getenv(name)

    if value is None or not value.strip():
        raise ValueError(
            f"Missing required environment variable: {name}"
        )

    return value.strip()


def _download_config(
    bucket_name: str,
    object_key: str,
    storage_region: str,
    local_path: Path,
) -> AtlasConfig:
    """Download Atlas TOML configuration from Amazon S3."""

    local_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    session = boto3.Session(
        region_name=storage_region,
    )

    s3 = session.client("s3")

    s3.download_file(
        Bucket=bucket_name,
        Key=object_key,
        Filename=str(local_path),
    )

    config = load_config(
        local_path
    )

    if config.atlas.bucket != bucket_name:
        raise ValueError(
            "Atlas bucket mismatch: "
            f"environment={bucket_name}, "
            f"config={config.atlas.bucket}"
        )

    if config.atlas.storage_region != storage_region:
        raise ValueError(
            "Atlas storage Region mismatch: "
            f"environment={storage_region}, "
            f"config={config.atlas.storage_region}"
        )

    return config


def _apply_runtime_overrides(
    config: AtlasConfig,
) -> AtlasConfig:
    """Apply Lambda-specific runtime settings."""

    output_root = os.getenv(
        "ATLAS_OUTPUT_ROOT",
        "/tmp/rds-discovery-metrics",
    ).strip()

    if not output_root:
        raise ValueError(
            "ATLAS_OUTPUT_ROOT must not be empty"
        )

    atlas_settings = replace(
        config.atlas,
        source_root=output_root,
    )

    return replace(
        config,
        atlas=atlas_settings,
    )


def lambda_handler(
    event: dict[str, Any] | None,
    context: Any,
) -> dict[str, Any]:
    """Run configured RDS discovery and metric collection."""

    bucket_name = _required_env(
        "ATLAS_BUCKET"
    )

    storage_region = _required_env(
        "ATLAS_STORAGE_REGION"
    )

    config_key = os.getenv(
        "ATLAS_CONFIG_KEY",
        "config/atlas.toml",
    ).strip()

    if not config_key:
        raise ValueError(
            "ATLAS_CONFIG_KEY must not be empty"
        )

    local_config_path = Path(
        os.getenv(
            "ATLAS_CONFIG_LOCAL_PATH",
            "/tmp/atlas.toml",
        )
    )

    started = time.perf_counter()

    logger.info(
        "collection_started",
        extra={
            "request_id": (
                context_request_id(context)
            ),
            "config_bucket": bucket_name,
            "config_key": config_key,
            "storage_region": storage_region,
        },
    )

    config = _download_config(
        bucket_name=bucket_name,
        object_key=config_key,
        storage_region=storage_region,
        local_path=local_config_path,
    )

    config = _apply_runtime_overrides(
        config
    )

    executions = run_configured_pipeline(
        config=config,
        base_profile_name=None,
        storage_profile_name=None,
        direct_target_profile_name=None,
    )

    uploaded_count = sum(
        execution["uploaded_count"]
        for execution in executions
    )

    discovered_instance_count = sum(
        execution["instance_count"]
        for execution in executions
    )

    selected_instance_count = sum(
        execution["selected_instance_count"]
        for execution in executions
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
        "collection_succeeded",
        extra={
            "request_id": request_id,
            "enabled_target_count": len(
                config.enabled_targets
            ),
            "execution_count": len(
                executions
            ),
            "discovered_instance_count": (
                discovered_instance_count
            ),
            "selected_instance_count": (
                selected_instance_count
            ),
            "uploaded_count": uploaded_count,
            "duration_ms": elapsed_ms(started),
        },
    )

    return {
        "status": "success",
        "request_id": request_id,
        "config_bucket": bucket_name,
        "config_key": config_key,
        "storage_region": storage_region,
        "enabled_target_count": len(
            config.enabled_targets
        ),
        "execution_count": len(
            executions
        ),
        "discovered_instance_count": (
            discovered_instance_count
        ),
        "selected_instance_count": (
            selected_instance_count
        ),
        "uploaded_count": uploaded_count,
        "executions": executions,
    }