"""Collect Amazon RDS CloudWatch metrics and upload them to Amazon S3."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from boto3.s3.transfer import S3UploadFailedError
from botocore.exceptions import BotoCoreError, ClientError, ProfileNotFound

from src.collectors.cloudwatch_metrics import (
    METRIC_CONFIG,
    collect_metrics,
    create_session,
    save_json,
)
from src.storage.s3_uploader import build_object_key, upload_file


def run_pipeline(
    profile_name: str | None,
    region_name: str,
    bucket_name: str,
    db_instance_identifier: str,
    metric_names: list[str],
    lookback_minutes: int,
    period_seconds: int,
    source_root: Path,
    prefix: str,
) -> list[dict[str, Any]]:
    """Collect metrics, save JSON files, and upload them to Amazon S3."""

    session = create_session(
        profile_name=profile_name,
        region_name=region_name,
    )

    payloads = collect_metrics(
        session=session,
        region_name=region_name,
        db_instance_identifier=db_instance_identifier,
        metric_names=metric_names,
        lookback_minutes=lookback_minutes,
        period_seconds=period_seconds,
    )

    results: list[dict[str, Any]] = []

    for payload in payloads:
        local_path = save_json(
            payload=payload,
            output_root=source_root,
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
        "--db-instance-identifier",
        required=True,
        help="Amazon RDS DB instance identifier",
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

    return parser.parse_args()


def main() -> None:
    """Run the CloudWatch-to-S3 pipeline."""

    args = parse_arguments()

    try:
        results = run_pipeline(
            profile_name=args.profile,
            region_name=args.region,
            bucket_name=args.bucket,
            db_instance_identifier=args.db_instance_identifier,
            metric_names=args.metrics,
            lookback_minutes=args.lookback_minutes,
            period_seconds=args.period_seconds,
            source_root=Path(args.source_root),
            prefix=args.prefix,
        )

        for result in results:
            print(f"Metric: {result['metric_name']}")
            print(f"Datapoints: {result['datapoint_count']}")
            print(f"Local file: {result['local_path']}")
            print(f"Uploaded: {result['s3_uri']}")
            print(f"Size: {result['content_length']} bytes")
            print(f"Encryption: {result['encryption']}")
            print("-" * 60)

        print(f"Pipeline completed: {len(results)} metrics uploaded")

    except ProfileNotFound as error:
        raise SystemExit(f"AWS profile not found: {error}") from error
    except ValueError as error:
        raise SystemExit(str(error)) from error
    except (
        ClientError,
        BotoCoreError,
        S3UploadFailedError,
    ) as error:
        raise SystemExit(f"Pipeline failed: {error}") from error


if __name__ == "__main__":
    main()