"""Build and run the Project Atlas DB Health Snapshot query."""

from __future__ import annotations

import argparse

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ProfileNotFound,
)

from src.query.athena_client import (
    AthenaQueryError,
    create_session,
    format_table,
    run_query,
)
from src.query.validators import (
    validate_account_id,
    validate_date,
    validate_region,
    validate_resource_id,
)

_DEFAULT_DATABASE = "project_atlas"
_DEFAULT_TABLE = "cloudwatch_metrics"

_DB_HEALTH_SNAPSHOT_TEMPLATE = """
SELECT
    resource_id,
    engine,
    cluster_role,
    ROUND(AVG(CASE WHEN metric_name = 'CPUUtilization' THEN value END), 2)
        AS cpu_avg,
    ROUND(MAX(CASE WHEN metric_name = 'CPUUtilization' THEN value END), 2)
        AS cpu_max,
    ROUND(MIN(CASE WHEN metric_name = 'FreeableMemory' THEN value END), 0)
        AS freeable_memory_min_bytes,
    ROUND(AVG(CASE WHEN metric_name = 'DatabaseConnections' THEN value END), 2)
        AS connections_avg,
    ROUND(MAX(CASE WHEN metric_name = 'DatabaseConnections' THEN value END), 2)
        AS connections_max,
    ROUND(AVG(CASE WHEN metric_name = 'ReadLatency' THEN value END), 4)
        AS read_latency_avg,
    ROUND(AVG(CASE WHEN metric_name = 'WriteLatency' THEN value END), 4)
        AS write_latency_avg,
    ROUND(AVG(CASE WHEN metric_name = 'ReadIOPS' THEN value END), 2)
        AS read_iops_avg,
    ROUND(AVG(CASE WHEN metric_name = 'WriteIOPS' THEN value END), 2)
        AS write_iops_avg,
    ROUND(AVG(CASE WHEN metric_name = 'DiskQueueDepth' THEN value END), 2)
        AS disk_queue_depth_avg,
    ROUND(MAX(CASE WHEN metric_name = 'AuroraReplicaLag' THEN value END), 2)
        AS aurora_replica_lag_max,
    ROUND(MIN(CASE WHEN metric_name = 'FreeStorageSpace' THEN value END), 0)
        AS free_storage_space_min_bytes
FROM {database}.{table}
WHERE account_id = '{account_id}'
  AND region = '{region}'
  AND date = '{date}'
  {resource_filter}
GROUP BY resource_id, engine, cluster_role
ORDER BY resource_id
"""


def build_db_health_query(
    account_id: str,
    region: str,
    date: str,
    resource_ids: list[str] | None = None,
    database: str = _DEFAULT_DATABASE,
    table: str = _DEFAULT_TABLE,
) -> str:
    """Build the DB Health Snapshot SQL for one account/region/day.

    Inputs are validated against strict patterns rather than passed through
    Athena's ExecutionParameters, since account_id/region/date/resource_ids
    will eventually be filled in from a Slack request rather than typed
    by hand. When resource_ids is given, the snapshot is narrowed to those
    resources; otherwise it covers every resource in the account/region.
    """

    validate_account_id(account_id)
    validate_region(region)
    validate_date(date)

    resource_filter = ""

    if resource_ids:
        for resource_id in resource_ids:
            validate_resource_id(resource_id)

        quoted_ids = ", ".join(
            f"'{resource_id}'"
            for resource_id in resource_ids
        )
        resource_filter = (
            f"AND resource_id IN ({quoted_ids})"
        )

    return _DB_HEALTH_SNAPSHOT_TEMPLATE.format(
        database=database,
        table=table,
        account_id=account_id,
        region=region,
        date=date,
        resource_filter=resource_filter,
    ).strip()


def run_db_health_snapshot(
    session: boto3.Session,
    output_location: str,
    account_id: str,
    region: str,
    date: str,
    resource_ids: list[str] | None = None,
    database: str = _DEFAULT_DATABASE,
    table: str = _DEFAULT_TABLE,
    workgroup: str = "primary",
) -> list[dict[str, str | None]]:
    """Run the DB Health Snapshot query and return one row per resource."""

    query = build_db_health_query(
        account_id=account_id,
        region=region,
        date=date,
        resource_ids=resource_ids,
        database=database,
        table=table,
    )

    return run_query(
        session=session,
        database=database,
        output_location=output_location,
        query=query,
        workgroup=workgroup,
    )


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Run the Project Atlas "
            "DB Health Snapshot query."
        )
    )

    parser.add_argument(
        "--profile",
        default="atlas-test",
        help="AWS CLI profile",
    )
    parser.add_argument(
        "--athena-region",
        default="ap-northeast-2",
        help="AWS Region running Athena",
    )
    parser.add_argument(
        "--database",
        default=_DEFAULT_DATABASE,
        help="Athena database name",
    )
    parser.add_argument(
        "--table",
        default=_DEFAULT_TABLE,
        help="Athena table name",
    )
    parser.add_argument(
        "--output-location",
        required=True,
        help="S3 location for Athena query results",
    )
    parser.add_argument(
        "--workgroup",
        default="primary",
        help="Athena workgroup",
    )
    parser.add_argument(
        "--account-id",
        required=True,
        help="Target account_id partition to query",
    )
    parser.add_argument(
        "--target-region",
        required=True,
        help="Target region partition to query",
    )
    parser.add_argument(
        "--date",
        required=True,
        help="Target date partition to query (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--resource-id",
        default=None,
        help=(
            "Narrow the snapshot to one resource_id "
            "(default: every resource in the account/region)"
        ),
    )

    return parser.parse_args()


def main() -> None:
    """Run the DB Health Snapshot CLI."""

    args = parse_arguments()

    try:
        session = create_session(
            profile_name=args.profile,
            region_name=args.athena_region,
        )

        rows = run_db_health_snapshot(
            session=session,
            output_location=args.output_location,
            account_id=args.account_id,
            region=args.target_region,
            date=args.date,
            resource_ids=(
                [args.resource_id]
                if args.resource_id
                else None
            ),
            database=args.database,
            table=args.table,
            workgroup=args.workgroup,
        )

    except ProfileNotFound as error:
        raise SystemExit(
            f"AWS profile not found: {error}"
        ) from error
    except ValueError as error:
        raise SystemExit(str(error)) from error
    except AthenaQueryError as error:
        raise SystemExit(str(error)) from error
    except (
        ClientError,
        BotoCoreError,
    ) as error:
        raise SystemExit(
            f"Athena query failed: {error}"
        ) from error

    print()
    print("=" * 60)
    print("PROJECT ATLAS DB HEALTH SNAPSHOT")
    print("=" * 60)
    print(
        f"account_id={args.account_id} "
        f"region={args.target_region} "
        f"date={args.date}"
    )
    print()
    print(f"{len(rows)} resource(s)")
    print()
    print(format_table(rows))


if __name__ == "__main__":
    main()
