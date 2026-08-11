"""Collect Amazon RDS CloudWatch metrics and upload them to Amazon S3."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from boto3.s3.transfer import S3UploadFailedError
from botocore.exceptions import BotoCoreError, ClientError, ProfileNotFound

from src.auth.aws_session import (
    create_target_session,
    get_session_account_id,
)
from src.collectors.cloudwatch_metrics import (
    METRIC_CONFIG,
    RESOURCE_DIMENSIONS,
    collect_metrics,
    create_session,
    save_json,
)
from src.storage.s3_uploader import build_object_key, upload_file


def run_pipeline(
    profile_name: str | None,
    region_name: str,
    bucket_name: str,
    resource_dimension: str,
    resource_id: str,
    metric_names: list[str],
    lookback_minutes: int,
    period_seconds: int,
    source_root: Path,
    prefix: str,
    target_role_arn: str | None = None,
    target_external_id: str | None = None,
    role_session_name: str = "project-atlas-cloudwatch",
) -> list[dict[str, Any]]:
    """Collect metrics, save JSON files, and upload them to Amazon S3."""

    base_session = create_session(
        profile_name=profile_name,
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

    payloads = collect_metrics(
        session=metric_session,
        region_name=region_name,
        resource_dimension=resource_dimension,
        resource_id=resource_id,
        metric_names=metric_names,
        lookback_minutes=lookback_minutes,
        period_seconds=period_seconds,
    )

    account_source_root = (
        source_root
        / f"account_id={source_account_id}"
    )

    results: list[dict[str, Any]] = []

    for payload in payloads:
        payload["source_account_id"] = source_account_id

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
            profile_name=profile_name,
            region_name=region_name,
            bucket_name=bucket_name,
            file_path=local_path,
            object_key=object_key,
        )

        results.append(
            {
                "source_account_id": source_account_id,
                "metric_name": payload["metric_name"],
                "datapoint_count": payload["datapoint_count"],
                "local_path": str(local_path),
                "s3_uri": f"s3://{bucket_name}/{object_key}",
                "content_length": metadata["ContentLength"],
                "encryption": metadata.get("ServerSideEncryption"),
            }
        )

    return results


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Collect Amazon RDS CloudWatch metrics "
            "and upload the resulting JSON files to Amazon S3."
        )
    )

    parser.add_argument(
        "--resource-id",
        "--db-instance-identifier",
        dest="resource_id",
        required=True,
        help="Amazon RDS DB instance or Aurora cluster identifier",
    )

    parser.add_argument(
        "--resource-dimension",
        choices=list(RESOURCE_DIMENSIONS),
        default="DBInstanceIdentifier",
        help="CloudWatch resource dimension",
    )

    parser.add_argument(
        "--bucket",
        required=True,
        help="Destination Amazon S3 bucket",
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
        "--metrics",
        nargs="+",
        choices=list(METRIC_CONFIG),
        default=list(METRIC_CONFIG),
        help="CloudWatch metrics to collect",
    )

    parser.add_argument(
        "--lookback-minutes",
        type=int,
        default=60,
        help="Metric lookback period in minutes",
    )

    parser.add_argument(
        "--period-seconds",
        type=int,
        default=300,
        help="CloudWatch aggregation period in seconds",
    )

    parser.add_argument(
        "--source-root",
        default="data/raw/cloudwatch",
        help="Local raw data root",
    )

    parser.add_argument(
        "--prefix",
        default="raw/cloudwatch",
        help="Destination S3 object prefix",
    )

    parser.add_argument(
        "--target-role-arn",
        default=None,
        help="Cross-account IAM role ARN used for metric collection",
    )

    parser.add_argument(
        "--target-external-id",
        default=None,
        help="Optional external ID used when assuming the target role",
    )

    parser.add_argument(
        "--role-session-name",
        default="project-atlas-cloudwatch",
        help="STS assumed-role session name",
    )

    return parser.parse_args()


def main() -> None:
    """Run the CloudWatch-to-S3 pipeline."""

    args = parse_arguments()

    try:
        results = run_pipeline(
            profile_name=args.profile,
            region_name=args.region,
            bucket_name=args.bucket,
            resource_dimension=args.resource_dimension,
            resource_id=args.resource_id,
            metric_names=args.metrics,
            lookback_minutes=args.lookback_minutes,
            period_seconds=args.period_seconds,
            source_root=Path(args.source_root),
            prefix=args.prefix,
            target_role_arn=args.target_role_arn,
            target_external_id=args.target_external_id,
            role_session_name=args.role_session_name,
        )

        for result in results:
            print(
                f"Source account: "
                f"{result['source_account_id']}"
            )
            print(f"Metric: {result['metric_name']}")
            print(f"Datapoints: {result['datapoint_count']}")
            print(f"Local file: {result['local_path']}")
            print(f"Uploaded: {result['s3_uri']}")
            print(f"Size: {result['content_length']} bytes")
            print(f"Encryption: {result['encryption']}")
            print("-" * 60)

        print(
            f"Pipeline completed: {len(results)} metrics uploaded"
        )

    except ProfileNotFound as error:
        raise SystemExit(
            f"AWS profile not found: {error}"
        ) from error

    except ValueError as error:
        raise SystemExit(str(error)) from error

    except (
        ClientError,
        BotoCoreError,
        S3UploadFailedError,
    ) as error:
        raise SystemExit(
            f"Pipeline failed: {error}"
        ) from error


if __name__ == "__main__":
    main()