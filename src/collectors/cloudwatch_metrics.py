"""Collect CPU utilization metrics for an Amazon RDS instance."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError, ProfileNotFound


def collect_cpu_utilization(
    profile_name: str,
    region_name: str,
    db_instance_identifier: str,
    lookback_minutes: int = 60,
    period_seconds: int = 300,
) -> dict:
    """Collect average CPU utilization from Amazon CloudWatch."""

    session = boto3.Session(
        profile_name=profile_name,
        region_name=region_name,
    )

    cloudwatch = session.client("cloudwatch")

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(minutes=lookback_minutes)

    response = cloudwatch.get_metric_data(
        MetricDataQueries=[
            {
                "Id": "cpu",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/RDS",
                        "MetricName": "CPUUtilization",
                        "Dimensions": [
                            {
                                "Name": "DBInstanceIdentifier",
                                "Value": db_instance_identifier,
                            }
                        ],
                    },
                    "Period": period_seconds,
                    "Stat": "Average",
                    "Unit": "Percent",
                },
                "ReturnData": True,
            }
        ],
        StartTime=start_time,
        EndTime=end_time,
        ScanBy="TimestampAscending",
    )

    result = response["MetricDataResults"][0]

    datapoints = [
        {
            "timestamp": timestamp.isoformat(),
            "value": round(float(value), 4),
        }
        for timestamp, value in zip(
            result.get("Timestamps", []),
            result.get("Values", []),
        )
    ]

    return {
        "schema_version": "1.0",
        "namespace": "AWS/RDS",
        "region": region_name,
        "resource_id": db_instance_identifier,
        "metric_name": "CPUUtilization",
        "statistic": "Average",
        "unit": "Percent",
        "period_seconds": period_seconds,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "datapoint_count": len(datapoints),
        "datapoints": datapoints,
    }


def save_json(payload: dict) -> Path:
    """Save metric data using a partitioned directory structure."""

    end_time = datetime.fromisoformat(payload["end_time"])

    output_path = (
        Path("data/raw/cloudwatch")
        / f"region={payload['region']}"
        / f"metric={payload['metric_name']}"
        / f"date={end_time:%Y-%m-%d}"
        / (
            f"{payload['resource_id']}_"
            f"{end_time:%Y%m%dT%H%M%SZ}.json"
        )
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2)

    return output_path


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--db-instance-identifier",
        required=True,
    )
    parser.add_argument(
        "--profile",
        default="atlas-test",
    )
    parser.add_argument(
        "--region",
        default="ap-northeast-2",
    )

    return parser.parse_args()


def main() -> None:
    """Run the CloudWatch metric collector."""

    args = parse_arguments()

    try:
        payload = collect_cpu_utilization(
            profile_name=args.profile,
            region_name=args.region,
            db_instance_identifier=args.db_instance_identifier,
        )

        output_path = save_json(payload)

        print(f"Metric: {payload['metric_name']}")
        print(f"Datapoints: {payload['datapoint_count']}")
        print(f"Output: {output_path}")

    except ProfileNotFound as error:
        raise SystemExit(f"AWS profile not found: {error}") from error
    except (ClientError, BotoCoreError) as error:
        raise SystemExit(f"AWS API request failed: {error}") from error


if __name__ == "__main__":
    main()