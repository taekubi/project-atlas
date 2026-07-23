"""Collect Amazon RDS DB instance inventory."""

from __future__ import annotations

import argparse
import sys
from typing import Any

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    ProfileNotFound,
)


def create_session(
    profile_name: str,
    region_name: str,
) -> boto3.Session:
    """Create an AWS session using a named profile."""

    return boto3.Session(
        profile_name=profile_name,
        region_name=region_name,
    )


def collect_rds_instances(
    session: boto3.Session,
) -> list[dict[str, Any]]:
    """Collect basic inventory for every RDS DB instance."""

    rds_client = session.client("rds")
    paginator = rds_client.get_paginator("describe_db_instances")

    instances: list[dict[str, Any]] = []

    for page in paginator.paginate():
        for db_instance in page.get("DBInstances", []):
            instances.append(
                {
                    "identifier": db_instance.get("DBInstanceIdentifier"),
                    "engine": db_instance.get("Engine"),
                    "engine_version": db_instance.get("EngineVersion"),
                    "instance_class": db_instance.get("DBInstanceClass"),
                    "availability_zone": db_instance.get(
                        "AvailabilityZone"
                    ),
                    "status": db_instance.get("DBInstanceStatus"),
                    "multi_az": db_instance.get("MultiAZ", False),
                    "storage_type": db_instance.get("StorageType"),
                    "performance_insights_enabled": db_instance.get(
                        "PerformanceInsightsEnabled",
                        False,
                    ),
                }
            )

    return instances


def print_inventory(
    instances: list[dict[str, Any]],
    region_name: str,
) -> None:
    """Print collected RDS inventory."""

    print(f"\nAWS Region: {region_name}")
    print(f"RDS instance count: {len(instances)}\n")

    if not instances:
        print("No RDS DB instances found.")
        return

    for index, instance in enumerate(instances, start=1):
        print(f"[{index}] {instance['identifier']}")
        print(
            f"    Engine       : "
            f"{instance['engine']} {instance['engine_version']}"
        )
        print(f"    Instance type: {instance['instance_class']}")
        print(f"    AZ           : {instance['availability_zone']}")
        print(f"    Status       : {instance['status']}")
        print(f"    Multi-AZ     : {instance['multi_az']}")
        print(f"    Storage type : {instance['storage_type']}")
        print(
            f"    PI enabled   : "
            f"{instance['performance_insights_enabled']}"
        )
        print()


def parse_arguments() -> argparse.Namespace:
    """Parse command-line options."""

    parser = argparse.ArgumentParser(
        description="Collect Amazon RDS DB instance inventory."
    )

    parser.add_argument(
        "--profile",
        default="atlas-test",
        help="AWS CLI profile name",
    )

    parser.add_argument(
        "--region",
        default="ap-northeast-2",
        help="AWS Region",
    )

    return parser.parse_args()


def main() -> int:
    """Run the RDS inventory collector."""

    args = parse_arguments()

    try:
        session = create_session(
            profile_name=args.profile,
            region_name=args.region,
        )

        caller_identity = session.client(
            "sts"
        ).get_caller_identity()

        print(
            "Authenticated account: "
            f"{caller_identity['Account']}"
        )

        instances = collect_rds_instances(session)

        print_inventory(
            instances=instances,
            region_name=args.region,
        )

        return 0

    except ProfileNotFound as error:
        print(
            f"AWS profile was not found: {error}",
            file=sys.stderr,
        )

    except NoCredentialsError:
        print(
            "AWS credentials were not found.",
            file=sys.stderr,
        )

    except ClientError as error:
        print(
            f"AWS API request failed: {error}",
            file=sys.stderr,
        )

    except BotoCoreError as error:
        print(
            f"AWS SDK error occurred: {error}",
            file=sys.stderr,
        )

    return 1


if __name__ == "__main__":
    raise SystemExit(main())