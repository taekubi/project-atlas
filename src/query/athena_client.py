"""Run Amazon Athena SQL queries for Project Atlas."""

from __future__ import annotations

import argparse
import time

import boto3
from botocore.client import BaseClient
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ProfileNotFound,
)

_FAILED_STATES = (
    "FAILED",
    "CANCELLED",
)

_DEFAULT_POLL_SECONDS = 1.0
_DEFAULT_TIMEOUT_SECONDS = 60.0
_DEFAULT_WORKGROUP = "primary"


class AthenaQueryError(Exception):
    """Raised when an Athena query does not succeed."""


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


def run_query(
    session: boto3.Session,
    database: str,
    output_location: str,
    query: str,
    workgroup: str = _DEFAULT_WORKGROUP,
    poll_seconds: float = _DEFAULT_POLL_SECONDS,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, str | None]]:
    """Run an Athena SQL query and return the result rows."""

    client = session.client("athena")

    query_execution_id = _start_query(
        client=client,
        database=database,
        output_location=output_location,
        query=query,
        workgroup=workgroup,
    )

    _wait_for_completion(
        client=client,
        query_execution_id=query_execution_id,
        poll_seconds=poll_seconds,
        timeout_seconds=timeout_seconds,
    )

    return _fetch_rows(
        client=client,
        query_execution_id=query_execution_id,
    )


def _start_query(
    client: BaseClient,
    database: str,
    output_location: str,
    query: str,
    workgroup: str,
) -> str:
    """Submit the query and return its execution ID."""

    response = client.start_query_execution(
        QueryString=query,
        QueryExecutionContext={
            "Database": database,
        },
        ResultConfiguration={
            "OutputLocation": output_location,
        },
        WorkGroup=workgroup,
    )

    return response["QueryExecutionId"]


def _wait_for_completion(
    client: BaseClient,
    query_execution_id: str,
    poll_seconds: float,
    timeout_seconds: float,
) -> None:
    """Poll the query execution until it reaches a terminal state."""

    elapsed_seconds = 0.0

    while True:
        response = client.get_query_execution(
            QueryExecutionId=query_execution_id,
        )

        status = response["QueryExecution"]["Status"]
        state = status["State"]

        if state == "SUCCEEDED":
            return

        if state in _FAILED_STATES:
            reason = status.get(
                "StateChangeReason",
                "no reason reported",
            )

            raise AthenaQueryError(
                f"Athena query {state.lower()}: {reason}"
            )

        if elapsed_seconds >= timeout_seconds:
            raise AthenaQueryError(
                "Athena query timed out after "
                f"{timeout_seconds:.0f} seconds "
                f"(last state: {state})"
            )

        time.sleep(poll_seconds)
        elapsed_seconds += poll_seconds


def _fetch_rows(
    client: BaseClient,
    query_execution_id: str,
) -> list[dict[str, str | None]]:
    """Fetch and flatten all result pages into row dictionaries.

    A page can legitimately carry no rows at all. A DDL statement such
    as MSCK REPAIR TABLE returns an empty result set when there was
    nothing to do -- so the curated refresh succeeds on the run that
    adds partitions and then returns nothing on the next one, an hour
    later, with no new data. Treating that as "no rows" rather than
    reading a header out of an empty list is what keeps the scheduled
    refresh from failing on every quiet hour.
    """

    columns: list[str] | None = None
    rows: list[dict[str, str | None]] = []

    paginator = client.get_paginator(
        "get_query_results"
    )

    for page in paginator.paginate(
        QueryExecutionId=query_execution_id,
    ):
        result_rows = page["ResultSet"]["Rows"]

        if not result_rows:
            continue

        start_index = 0

        if columns is None:
            columns = [
                field.get("VarCharValue", "")
                for field in result_rows[0]["Data"]
            ]
            start_index = 1

        for row in result_rows[start_index:]:
            values = [
                field.get("VarCharValue")
                for field in row["Data"]
            ]

            rows.append(
                dict(zip(columns, values))
            )

    return rows


def format_table(
    rows: list[dict[str, str | None]],
) -> str:
    """Render query result rows as an aligned text table."""

    if not rows:
        return "(no rows)"

    columns = list(rows[0].keys())

    widths = {
        column: max(
            len(column),
            max(
                len(str(row.get(column, "")))
                for row in rows
            ),
        )
        for column in columns
    }

    header = "  ".join(
        column.ljust(widths[column])
        for column in columns
    )
    separator = "  ".join(
        "-" * widths[column]
        for column in columns
    )
    body_lines = [
        "  ".join(
            str(row.get(column, "") or "").ljust(
                widths[column]
            )
            for column in columns
        )
        for row in rows
    ]

    return "\n".join(
        [header, separator, *body_lines]
    )


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Run an ad-hoc Amazon Athena "
            "SQL query for Project Atlas."
        )
    )

    parser.add_argument(
        "--profile",
        default="atlas-test",
        help="AWS CLI profile",
    )
    parser.add_argument(
        "--region",
        default="ap-northeast-2",
        help="AWS Region running Athena",
    )
    parser.add_argument(
        "--database",
        required=True,
        help="Athena database name",
    )
    parser.add_argument(
        "--output-location",
        required=True,
        help="S3 location for Athena query results",
    )
    parser.add_argument(
        "--workgroup",
        default=_DEFAULT_WORKGROUP,
        help="Athena workgroup",
    )
    parser.add_argument(
        "--query",
        required=True,
        help="SQL query to run",
    )

    return parser.parse_args()


def main() -> None:
    """Run the Athena query CLI."""

    args = parse_arguments()

    try:
        session = create_session(
            profile_name=args.profile,
            region_name=args.region,
        )

        rows = run_query(
            session=session,
            database=args.database,
            output_location=args.output_location,
            query=args.query,
            workgroup=args.workgroup,
        )

    except ProfileNotFound as error:
        raise SystemExit(
            f"AWS profile not found: {error}"
        ) from error
    except AthenaQueryError as error:
        raise SystemExit(str(error)) from error
    except (
        ClientError,
        BotoCoreError,
    ) as error:
        raise SystemExit(
            f"Athena query failed: {error}"
        ) from error

    print(f"{len(rows)} row(s)")
    print()
    print(format_table(rows))


if __name__ == "__main__":
    main()
