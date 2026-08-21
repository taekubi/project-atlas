"""Discover RDS DB instances and collect CloudWatch metrics."""

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
    METRIC_CONFIG,
    collect_metrics,
    save_json,
)
from src.collectors.rds_inventory import (
    collect_rds_inventory,
)
from src.storage.s3_uploader import (
    build_object_key,
    upload_file,
)


def create_session(
    profile_name: str | None,
    region_name: str,
) -> boto3.Session:
    """Create a local or Lambda-compatible AWS session."""

    if profile_name:
        return boto3.Session(
            profile_name=profile_name,
            region_name=region_name,
        )

    return boto3.Session(
        region_name=region_name,
    )


def select_available_instances(
    inventory: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return available DB instances that can be monitored."""

    resources: list[dict[str, Any]] = []

    for instance in inventory["instances"]:
        resource_id = instance.get("resource_id")
        status = instance.get("status")

        if not resource_id:
            continue

        if status != "available":
            continue

        resources.append(instance)

    return resources


def select_available_clusters(
    inventory: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return available DB clusters that can be monitored."""

    resources: list[dict[str, Any]] = []

    for cluster in inventory["clusters"]:
        identifier = cluster.get("identifier")
        status = cluster.get("status")

        if not identifier:
            continue

        if status != "available":
            continue

        resources.append(cluster)

    return resources


def run_discovery_metrics_pipeline(
    source_profile_name: str | None,
    storage_profile_name: str | None,
    region_name: str,
    bucket_name: str,
    metric_names: list[str],
    lookback_minutes: int,
    period_seconds: int,
    source_root: Path,
    prefix: str,
    target_role_arn: str | None = None,
    target_external_id: str | None = None,
    role_session_name: str = (
        "project-atlas-rds-discovery"
    ),
) -> dict[str, Any]:
    """Discover DB instances, collect metrics, and upload them."""

    base_session = create_session(
        profile_name=source_profile_name,
        region_name=region_name,
    )

    metric_session = create_target_session(
        base_session=base_session,
        region_name=region_name,
        role_arn=target_role_arn,
        role_session_name=role_session_name,
        external_id=target_external_id,
    )

    source_account_id = get_session_account_id(
        session=metric_session,
        region_name=region_name,
    )

    inventory = collect_rds_inventory(
        session=metric_session,
    )

    resources = select_available_instances(
        inventory=inventory,
    )

    account_source_root = (
        source_root
        / f"account_id={source_account_id}"
    )

    results: list[dict[str, Any]] = []

    for resource in resources:
        resource_id = resource["resource_id"]

        payloads = collect_metrics(
            session=metric_session,
            region_name=region_name,
            resource_dimension="DBInstanceIdentifier",
            resource_id=resource_id,
            metric_names=metric_names,
            lookback_minutes=lookback_minutes,
            period_seconds=period_seconds,
        )

        for payload in payloads:
            payload["source_account_id"] = (
                source_account_id
            )

            payload["engine"] = resource.get(
                "engine"
            )

            payload["cluster_identifier"] = (
                resource.get(
                    "cluster_identifier"
                )
            )

            payload["cluster_role"] = resource.get(
                "cluster_role"
            )

            local_path = save_json(
                payload=payload,
                output_root=account_source_root,
            )

            object_key = build_object_key(
                file_path=local_path,
                source_root=source_root,
                prefix=prefix,
            )

            metadata = upload_file(
                profile_name=storage_profile_name,
                region_name=region_name,
                bucket_name=bucket_name,
                file_path=local_path,
                object_key=object_key,
            )

            results.append(
                {
                    "source_account_id": (
                        source_account_id
                    ),
                    "resource_id": resource_id,
                    "engine": resource.get(
                        "engine"
                    ),
                    "cluster_identifier": (
                        resource.get(
                            "cluster_identifier"
                        )
                    ),
                    "cluster_role": resource.get(
                        "cluster_role"
                    ),
                    "metric_name": payload[
                        "metric_name"
                    ],
                    "datapoint_count": payload[
                        "datapoint_count"
                    ],
                    "s3_uri": (
                        f"s3://{bucket_name}/"
                        f"{object_key}"
                    ),
                    "content_length": metadata[
                        "ContentLength"
                    ],
                    "encryption": metadata.get(
                        "ServerSideEncryption"
                    ),
                }
            )

    return {
        "source_account_id": source_account_id,
        "cluster_count": len(
            inventory["clusters"]
        ),
        "discovered_instance_count": len(
            inventory["instances"]
        ),
        "selected_instance_count": len(
            resources
        ),
        "uploaded_count": len(results),
        "results": results,
    }


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Automatically discover RDS DB instances "
            "and collect CloudWatch metrics."
        )
    )

    parser.add_argument(
        "--source-profile",
        default="atlas-test",
        help="AWS profile used to read source metrics",
    )

    parser.add_argument(
        "--storage-profile",
        default="atlas-test",
        help="AWS profile used to write Atlas S3 data",
    )

    parser.add_argument(
        "--region",
        default="ap-northeast-2",
        help="AWS Region",
    )

    parser.add_argument(
        "--bucket",
        required=True,
        help="Atlas S3 bucket",
    )

    parser.add_argument(
        "--metrics",
        nargs="+",
        choices=list(METRIC_CONFIG),
        default=["CPUUtilization"],
        help="CloudWatch metrics to collect",
    )

    parser.add_argument(
        "--lookback-minutes",
        type=int,
        default=15,
        help="Metric lookback period",
    )

    parser.add_argument(
        "--period-seconds",
        type=int,
        default=300,
        help="CloudWatch period",
    )

    parser.add_argument(
        "--source-root",
        default="tmp/rds-discovery-metrics",
        help="Local output root",
    )

    parser.add_argument(
        "--prefix",
        default="raw/cloudwatch",
        help="S3 object prefix",
    )

    parser.add_argument(
        "--target-role-arn",
        default=None,
        help="Optional target-account IAM role ARN",
    )

    parser.add_argument(
        "--target-external-id",
        default=None,
        help="Optional STS External ID",
    )

    parser.add_argument(
        "--role-session-name",
        default="project-atlas-rds-discovery",
        help="STS role session name",
    )

    return parser.parse_args()


def main() -> None:
    """Run discovery-based metric collection."""

    args = parse_arguments()

    try:
        result = run_discovery_metrics_pipeline(
            source_profile_name=args.source_profile,
            storage_profile_name=args.storage_profile,
            region_name=args.region,
            bucket_name=args.bucket,
            metric_names=args.metrics,
            lookback_minutes=args.lookback_minutes,
            period_seconds=args.period_seconds,
            source_root=Path(args.source_root),
            prefix=args.prefix,
            target_role_arn=args.target_role_arn,
            target_external_id=(
                args.target_external_id
            ),
            role_session_name=(
                args.role_session_name
            ),
        )

        print()
        print("=" * 60)
        print("PROJECT ATLAS DISCOVERY METRIC PIPELINE")
        print("=" * 60)

        print(
            "Source account      : "
            f"{result['source_account_id']}"
        )

        print(
            "DB clusters         : "
            f"{result['cluster_count']}"
        )

        print(
            "Discovered instances: "
            f"{result['discovered_instance_count']}"
        )

        print(
            "Selected instances  : "
            f"{result['selected_instance_count']}"
        )

        print(
            "Uploaded metrics    : "
            f"{result['uploaded_count']}"
        )

        print()

        for item in result["results"]:
            role = (
                item["cluster_role"]
                or "standalone"
            )

            print(
                f"{item['resource_id']} "
                f"[{role}]"
            )

            print(
                f"    Engine    : "
                f"{item['engine']}"
            )

            print(
                f"    Metric    : "
                f"{item['metric_name']}"
            )

            print(
                f"    Datapoints: "
                f"{item['datapoint_count']}"
            )

            print(
                f"    S3        : "
                f"{item['s3_uri']}"
            )

            print(
                f"    Encryption: "
                f"{item['encryption']}"
            )

            print()

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
            f"Discovery pipeline failed: {error}"
        ) from error


if __name__ == "__main__":
    main()