"""Run RDS metric collection from Project Atlas configuration."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import boto3
from boto3.s3.transfer import S3UploadFailedError
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ProfileNotFound,
)

from src.auth.aws_session import (
    create_target_session,
    get_session_account_id,
)
from src.collectors.cloudwatch_metrics import (
    collect_metrics,
    save_json,
)
from src.collectors.rds_inventory import (
    collect_rds_inventory,
)
from src.collectors.metric_profiles import (
    resolve_metric_profile,
)
from src.config.atlas_config import (
    AtlasConfig,
    TargetSettings,
    load_config,
)
from src.pipelines.rds_discovery_metrics_pipeline import (
    select_available_instances,
)
from src.storage.s3_uploader import (
    build_object_key,
    upload_file,
)


def create_session(
    profile_name: str | None,
    region_name: str,
) -> boto3.Session:
    """Create an AWS session for local or Lambda execution."""

    if profile_name:
        return boto3.Session(
            profile_name=profile_name,
            region_name=region_name,
        )

    return boto3.Session(
        region_name=region_name,
    )


def create_source_session(
    target: TargetSettings,
    source_region: str,
    base_profile_name: str | None,
    direct_target_profile_name: str | None,
) -> boto3.Session:
    """Create the AWS session used to read a target account."""

    if direct_target_profile_name:
        return create_session(
            profile_name=direct_target_profile_name,
            region_name=source_region,
        )

    base_session = create_session(
        profile_name=base_profile_name,
        region_name=source_region,
    )

    return create_target_session(
        base_session=base_session,
        region_name=source_region,
        role_arn=target.role_arn,
        role_session_name=(
            "project-atlas-configured-collection"
        ),
    )


def collect_target_region(
    config: AtlasConfig,
    target: TargetSettings,
    source_region: str,
    base_profile_name: str | None,
    storage_profile_name: str | None,
    direct_target_profile_name: str | None,
) -> dict[str, Any]:
    """Collect all available DB instances in one target Region."""

    source_session = create_source_session(
        target=target,
        source_region=source_region,
        base_profile_name=base_profile_name,
        direct_target_profile_name=(
            direct_target_profile_name
        ),
    )

    actual_account_id = get_session_account_id(
        session=source_session,
        region_name=source_region,
    )

    if actual_account_id != target.account_id:
        raise ValueError(
            "Target account mismatch: "
            f"config={target.account_id}, "
            f"session={actual_account_id}"
        )

    inventory = collect_rds_inventory(
        session=source_session,
    )

    resources = select_available_instances(
        inventory=inventory,
    )

    source_root = Path(
        config.atlas.source_root
    )

    account_source_root = (
        source_root
        / f"account_id={actual_account_id}"
    )

    results: list[dict[str, Any]] = []
    resource_summaries: list[dict[str, Any]] = []

    for resource in resources:
        resource_id = resource["resource_id"]

        if config.collection.uses_metric_profile:
            profile_name = (
                config.collection.metric_profile
            )

            if profile_name is None:
                raise ValueError(
                    "Metric profile configuration "
                    "is unexpectedly empty"
                )

            profile_selection = (
                resolve_metric_profile(
                    resource=resource,
                    profile_name=profile_name,
                )
            )

            metric_names = list(
                profile_selection.metrics
            )

            metric_profile = (
                profile_selection.profile_name
            )

            resource_profile = (
                profile_selection.resource_profile
            )

        else:
            metric_names = list(
                config.collection.metrics
            )

            metric_profile = None
            resource_profile = (
                "explicit-metrics"
            )

        resource_summaries.append(
            {
                "resource_id": resource_id,
                "engine": resource.get(
                    "engine"
                ),
                "cluster_role": resource.get(
                    "cluster_role"
                ),
                "metric_profile": metric_profile,
                "resource_profile": (
                    resource_profile
                ),
                "metric_count": len(
                    metric_names
                ),
                "metrics": metric_names,
            }
        )

        payloads = collect_metrics(
            session=source_session,
            region_name=source_region,
            resource_dimension="DBInstanceIdentifier",
            resource_id=resource_id,
            metric_names=metric_names,
            lookback_minutes=(
                config.collection.lookback_minutes
            ),
            period_seconds=(
                config.collection.period_seconds
            ),
        )

        for payload in payloads:
            payload["source_account_id"] = (
                actual_account_id
            )

            payload["target_name"] = (
                target.name
            )

            payload["engine"] = resource.get(
                "engine"
            )

            payload["cluster_identifier"] = (
                resource.get(
                    "cluster_identifier"
                )
            )

            payload["cluster_role"] = (
                resource.get(
                    "cluster_role"
                )
            )

            payload["metric_profile"] = (
                metric_profile
            )

            payload["resource_profile"] = (
                resource_profile
            )

            local_path = save_json(
                payload=payload,
                output_root=account_source_root,
            )

            object_key = build_object_key(
                file_path=local_path,
                source_root=source_root,
                prefix=config.atlas.s3_prefix,
            )

            metadata = upload_file(
                profile_name=storage_profile_name,
                region_name=(
                    config.atlas.storage_region
                ),
                bucket_name=config.atlas.bucket,
                file_path=local_path,
                object_key=object_key,
            )

            results.append(
                {
                    "target_name": target.name,
                    "source_account_id": (
                        actual_account_id
                    ),
                    "source_region": (
                        source_region
                    ),
                    "storage_region": (
                        config.atlas.storage_region
                    ),
                    "resource_id": resource_id,
                    "engine": resource.get(
                        "engine"
                    ),
                    "cluster_role": resource.get(
                        "cluster_role"
                    ),
                    "metric_profile": metric_profile,
                    "resource_profile": (
                        resource_profile
                    ),
                    "metric_name": payload[
                        "metric_name"
                    ],
                    "datapoint_count": payload[
                        "datapoint_count"
                    ],
                    "s3_uri": (
                        f"s3://{config.atlas.bucket}/"
                        f"{object_key}"
                    ),
                    "encryption": metadata.get(
                        "ServerSideEncryption"
                    ),
                }
            )

    return {
        "target_name": target.name,
        "source_account_id": actual_account_id,
        "source_region": source_region,
        "cluster_count": len(
            inventory["clusters"]
        ),
        "instance_count": len(
            inventory["instances"]
        ),
        "selected_instance_count": len(
            resources
        ),
        "resource_summaries": (
            resource_summaries
        ),
        "uploaded_count": len(results),
        "results": results,
    }


def run_configured_pipeline(
    config: AtlasConfig,
    base_profile_name: str | None,
    storage_profile_name: str | None,
    direct_target_profile_name: str | None = None,
) -> list[dict[str, Any]]:
    """Run collection for all enabled target accounts and Regions."""

    enabled_targets = config.enabled_targets

    if (
        direct_target_profile_name
        and len(enabled_targets) != 1
    ):
        raise ValueError(
            "--target-profile can only be used "
            "when exactly one target is enabled"
        )

    executions: list[dict[str, Any]] = []

    for target in enabled_targets:
        for source_region in target.regions:
            result = collect_target_region(
                config=config,
                target=target,
                source_region=source_region,
                base_profile_name=base_profile_name,
                storage_profile_name=(
                    storage_profile_name
                ),
                direct_target_profile_name=(
                    direct_target_profile_name
                ),
            )

            executions.append(result)

    return executions


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Run Project Atlas metric collection "
            "using a TOML configuration file."
        )
    )

    parser.add_argument(
        "--config",
        required=True,
        help="Project Atlas TOML configuration file",
    )

    parser.add_argument(
        "--base-profile",
        default="atlas-test",
        help=(
            "AWS profile used before "
            "cross-account AssumeRole"
        ),
    )

    parser.add_argument(
        "--storage-profile",
        default="atlas-test",
        help="AWS profile used to write Atlas S3 data",
    )

    parser.add_argument(
        "--target-profile",
        default=None,
        help=(
            "Optional local-development profile "
            "that directly accesses the target account"
        ),
    )

    return parser.parse_args()


def main() -> None:
    """Run configured Project Atlas collection."""

    args = parse_arguments()

    try:
        config = load_config(
            args.config
        )

        executions = run_configured_pipeline(
            config=config,
            base_profile_name=args.base_profile,
            storage_profile_name=(
                args.storage_profile
            ),
            direct_target_profile_name=(
                args.target_profile
            ),
        )

        print()
        print("=" * 60)
        print("PROJECT ATLAS CONFIGURED COLLECTION")
        print("=" * 60)

        total_uploaded = 0

        for execution in executions:
            print()
            print(
                "Target         : "
                f"{execution['target_name']}"
            )

            print(
                "Account        : "
                f"{execution['source_account_id']}"
            )

            print(
                "Source region  : "
                f"{execution['source_region']}"
            )

            print(
                "DB clusters    : "
                f"{execution['cluster_count']}"
            )

            print(
                "DB instances   : "
                f"{execution['instance_count']}"
            )

            print(
                "Selected       : "
                f"{execution['selected_instance_count']}"
            )

            print(
                "Uploaded       : "
                f"{execution['uploaded_count']}"
            )

            total_uploaded += (
                execution["uploaded_count"]
            )

            for item in execution["results"]:
                role = (
                    item["cluster_role"]
                    or "standalone"
                )

                print(
                    "  - "
                    f"{item['resource_id']} "
                    f"[{role}] "
                    f"{item['metric_name']} "
                    f"({item['datapoint_count']} datapoints)"
                )

        print()
        print(
            "Total uploaded metrics: "
            f"{total_uploaded}"
        )

    except FileNotFoundError as error:
        raise SystemExit(
            str(error)
        ) from error

    except ProfileNotFound as error:
        raise SystemExit(
            f"AWS profile not found: {error}"
        ) from error

    except ValueError as error:
        raise SystemExit(
            str(error)
        ) from error

    except (
        ClientError,
        BotoCoreError,
        S3UploadFailedError,
    ) as error:
        raise SystemExit(
            f"Configured pipeline failed: {error}"
        ) from error


if __name__ == "__main__":
    main()