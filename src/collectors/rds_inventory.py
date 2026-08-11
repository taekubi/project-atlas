"""Discover Amazon RDS DB instances and DB clusters."""

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

from src.auth.aws_session import (
    create_target_session,
    get_session_account_id,
)


def create_session(
    profile_name: str | None,
    region_name: str,
) -> boto3.Session:
    """Create a base AWS session."""

    return boto3.Session(
        profile_name=profile_name,
        region_name=region_name,
    )


def collect_rds_clusters(
    session: boto3.Session,
) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, str]],
]:
    """Collect RDS DB clusters and build DB instance role mappings."""

    rds_client = session.client("rds")
    paginator = rds_client.get_paginator(
        "describe_db_clusters"
    )

    clusters: list[dict[str, Any]] = []
    instance_roles: dict[str, dict[str, str]] = {}

    for page in paginator.paginate():
        for db_cluster in page.get("DBClusters", []):
            cluster_identifier = db_cluster.get(
                "DBClusterIdentifier"
            )

            members: list[dict[str, Any]] = []

            for member in db_cluster.get(
                "DBClusterMembers",
                [],
            ):
                instance_identifier = member.get(
                    "DBInstanceIdentifier"
                )

                is_writer = member.get(
                    "IsClusterWriter",
                    False,
                )

                cluster_role = (
                    "writer"
                    if is_writer
                    else "reader"
                )

                members.append(
                    {
                        "identifier": instance_identifier,
                        "role": cluster_role,
                        "promotion_tier": member.get(
                            "PromotionTier"
                        ),
                    }
                )

                if instance_identifier:
                    instance_roles[instance_identifier] = {
                        "cluster_identifier": (
                            cluster_identifier
                            or ""
                        ),
                        "cluster_role": cluster_role,
                    }

            clusters.append(
                {
                    "resource_type": "db_cluster",
                    "resource_dimension": (
                        "DBClusterIdentifier"
                    ),
                    "resource_id": cluster_identifier,
                    "identifier": cluster_identifier,
                    "engine": db_cluster.get("Engine"),
                    "engine_version": db_cluster.get(
                        "EngineVersion"
                    ),
                    "status": db_cluster.get("Status"),
                    "members": members,
                }
            )

    return clusters, instance_roles


def collect_rds_instances(
    session: boto3.Session,
    instance_roles: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Collect RDS DB instances and their cluster roles."""

    rds_client = session.client("rds")
    paginator = rds_client.get_paginator(
        "describe_db_instances"
    )

    instances: list[dict[str, Any]] = []

    for page in paginator.paginate():
        for db_instance in page.get(
            "DBInstances",
            [],
        ):
            identifier = db_instance.get(
                "DBInstanceIdentifier"
            )

            role_info = instance_roles.get(
                identifier or "",
                {},
            )

            cluster_identifier = (
                db_instance.get(
                    "DBClusterIdentifier"
                )
                or role_info.get(
                    "cluster_identifier"
                )
            )

            cluster_role = role_info.get(
                "cluster_role"
            )

            instances.append(
                {
                    "resource_type": "db_instance",
                    "resource_dimension": (
                        "DBInstanceIdentifier"
                    ),
                    "resource_id": identifier,
                    "identifier": identifier,
                    "engine": db_instance.get("Engine"),
                    "engine_version": db_instance.get(
                        "EngineVersion"
                    ),
                    "instance_class": db_instance.get(
                        "DBInstanceClass"
                    ),
                    "availability_zone": db_instance.get(
                        "AvailabilityZone"
                    ),
                    "status": db_instance.get(
                        "DBInstanceStatus"
                    ),
                    "multi_az": db_instance.get(
                        "MultiAZ",
                        False,
                    ),
                    "storage_type": db_instance.get(
                        "StorageType"
                    ),
                    "performance_insights_enabled": (
                        db_instance.get(
                            "PerformanceInsightsEnabled",
                            False,
                        )
                    ),
                    "cluster_identifier": (
                        cluster_identifier
                    ),
                    "cluster_role": cluster_role,
                }
            )

    return instances


def collect_rds_inventory(
    session: boto3.Session,
) -> dict[str, list[dict[str, Any]]]:
    """Collect DB clusters and DB instances."""

    clusters, instance_roles = collect_rds_clusters(
        session=session,
    )

    instances = collect_rds_instances(
        session=session,
        instance_roles=instance_roles,
    )

    return {
        "clusters": clusters,
        "instances": instances,
    }


def print_inventory(
    inventory: dict[str, list[dict[str, Any]]],
    account_id: str,
    region_name: str,
) -> None:
    """Print discovered RDS resources."""

    clusters = inventory["clusters"]
    instances = inventory["instances"]

    print()
    print("=" * 60)
    print("PROJECT ATLAS RDS RESOURCE DISCOVERY")
    print("=" * 60)
    print(f"AWS Account : {account_id}")
    print(f"AWS Region  : {region_name}")
    print(f"DB Clusters : {len(clusters)}")
    print(f"DB Instances: {len(instances)}")

    print()
    print("=" * 60)
    print("DB CLUSTERS")
    print("=" * 60)

    if not clusters:
        print("No RDS DB clusters found.")
    else:
        for index, cluster in enumerate(
            clusters,
            start=1,
        ):
            print(
                f"[{index}] "
                f"{cluster['identifier']}"
            )
            print(
                f"    Engine : "
                f"{cluster['engine']} "
                f"{cluster['engine_version']}"
            )
            print(
                f"    Status : "
                f"{cluster['status']}"
            )

            members = cluster["members"]

            if not members:
                print("    Members: none")
            else:
                print("    Members:")

                for member in members:
                    print(
                        "      - "
                        f"{member['identifier']} "
                        f"({member['role']})"
                    )

            print()

    print("=" * 60)
    print("DB INSTANCES")
    print("=" * 60)

    if not instances:
        print("No RDS DB instances found.")
        return

    for index, instance in enumerate(
        instances,
        start=1,
    ):
        print(
            f"[{index}] "
            f"{instance['identifier']}"
        )

        print(
            f"    Engine       : "
            f"{instance['engine']} "
            f"{instance['engine_version']}"
        )

        print(
            f"    Instance type: "
            f"{instance['instance_class']}"
        )

        print(
            f"    AZ           : "
            f"{instance['availability_zone']}"
        )

        print(
            f"    Status       : "
            f"{instance['status']}"
        )

        if instance["cluster_identifier"]:
            print(
                f"    Cluster      : "
                f"{instance['cluster_identifier']}"
            )

            print(
                f"    Cluster role : "
                f"{instance['cluster_role']}"
            )

        else:
            print(
                "    Cluster      : standalone"
            )

        print(
            f"    Multi-AZ     : "
            f"{instance['multi_az']}"
        )

        print(
            f"    Storage type : "
            f"{instance['storage_type']}"
        )

        print(
            f"    PI enabled   : "
            f"{instance['performance_insights_enabled']}"
        )

        print()


def parse_arguments() -> argparse.Namespace:
    """Parse command-line options."""

    parser = argparse.ArgumentParser(
        description=(
            "Discover Amazon RDS DB instances "
            "and DB clusters."
        )
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

    parser.add_argument(
        "--target-role-arn",
        default=None,
        help=(
            "Optional cross-account IAM role ARN "
            "used for RDS discovery"
        ),
    )

    parser.add_argument(
        "--target-external-id",
        default=None,
        help=(
            "Optional external ID used when "
            "assuming the target role"
        ),
    )

    parser.add_argument(
        "--role-session-name",
        default="project-atlas-rds-discovery",
        help="STS assumed-role session name",
    )

    return parser.parse_args()


def main() -> int:
    """Run RDS resource discovery."""

    args = parse_arguments()

    try:
        base_session = create_session(
            profile_name=args.profile,
            region_name=args.region,
        )

        discovery_session = create_target_session(
            base_session=base_session,
            region_name=args.region,
            role_arn=args.target_role_arn,
            role_session_name=args.role_session_name,
            external_id=args.target_external_id,
        )

        account_id = get_session_account_id(
            session=discovery_session,
            region_name=args.region,
        )

        inventory = collect_rds_inventory(
            session=discovery_session,
        )

        print_inventory(
            inventory=inventory,
            account_id=account_id,
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

    except ValueError as error:
        print(
            str(error),
            file=sys.stderr,
        )

    return 1


if __name__ == "__main__":
    raise SystemExit(main())