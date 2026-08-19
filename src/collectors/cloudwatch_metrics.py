"""Collect Amazon RDS metrics from Amazon CloudWatch."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ProfileNotFound,
)

from src.collectors.metric_profiles import (
    METRIC_DEFINITIONS,
)


# Backward-compatible alias used by existing collectors and handlers.
METRIC_CONFIG = METRIC_DEFINITIONS


RESOURCE_DIMENSIONS: dict[str, str] = {
    "DBInstanceIdentifier": "DBInstance",
    "DBClusterIdentifier": "DBCluster",
}


def create_session(
    region_name: str,
    profile_name: str | None = None,
) -> boto3.Session:
    """Create a Boto3 session."""

    if profile_name:
        return boto3.Session(
            profile_name=profile_name,
            region_name=region_name,
        )

    return boto3.Session(
        region_name=region_name,
    )


def collect_metrics(
    session: boto3.Session,
    region_name: str,
    resource_dimension: str,
    resource_id: str,
    metric_names: list[str],
    lookback_minutes: int = 60,
    period_seconds: int = 300,
) -> list[dict[str, Any]]:
    """Collect multiple Amazon RDS CloudWatch metrics."""

    if resource_dimension not in RESOURCE_DIMENSIONS:
        raise ValueError(
            "Unsupported resource dimension: "
            f"{resource_dimension}"
        )

    if not metric_names:
        raise ValueError(
            "At least one metric name is required"
        )

    unsupported_metrics = [
        metric_name
        for metric_name in metric_names
        if metric_name not in METRIC_CONFIG
    ]

    if unsupported_metrics:
        raise ValueError(
            "Unsupported CloudWatch metrics: "
            + ", ".join(unsupported_metrics)
        )

    cloudwatch = session.client(
        "cloudwatch"
    )

    collected_at = datetime.now(
        timezone.utc
    )

    end_time = collected_at
    start_time = (
        end_time
        - timedelta(
            minutes=lookback_minutes
        )
    )

    metric_queries: list[
        dict[str, Any]
    ] = []

    query_id_to_metric: dict[
        str,
        str,
    ] = {}

    for index, metric_name in enumerate(
        metric_names
    ):
        query_id = f"m{index}"

        query_id_to_metric[
            query_id
        ] = metric_name

        metric_definition = (
            METRIC_CONFIG[
                metric_name
            ]
        )

        metric_queries.append(
            {
                "Id": query_id,
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/RDS",
                        "MetricName": (
                            metric_name
                        ),
                        "Dimensions": [
                            {
                                "Name": (
                                    resource_dimension
                                ),
                                "Value": (
                                    resource_id
                                ),
                            }
                        ],
                    },
                    "Period": (
                        period_seconds
                    ),
                    "Stat": (
                        metric_definition[
                            "statistic"
                        ]
                    ),
                },
                "ReturnData": True,
            }
        )

    request: dict[str, Any] = {
        "MetricDataQueries": (
            metric_queries
        ),
        "StartTime": start_time,
        "EndTime": end_time,
        "ScanBy": (
            "TimestampAscending"
        ),
    }

    metric_datapoints: dict[
        str,
        list[dict[str, Any]],
    ] = {
        metric_name: []
        for metric_name in metric_names
    }

    while True:
        response = (
            cloudwatch.get_metric_data(
                **request
            )
        )

        for result in response.get(
            "MetricDataResults",
            [],
        ):
            metric_name = (
                query_id_to_metric[
                    result["Id"]
                ]
            )

            timestamps = result.get(
                "Timestamps",
                [],
            )

            values = result.get(
                "Values",
                [],
            )

            for timestamp, value in zip(
                timestamps,
                values,
            ):
                metric_datapoints[
                    metric_name
                ].append(
                    {
                        "timestamp": (
                            timestamp
                            .astimezone(
                                timezone.utc
                            )
                            .isoformat()
                            .replace(
                                "+00:00",
                                "Z",
                            )
                        ),
                        "value": round(
                            float(value),
                            4,
                        ),
                    }
                )

        next_token = response.get(
            "NextToken"
        )

        if not next_token:
            break

        request["NextToken"] = (
            next_token
        )

    payloads: list[
        dict[str, Any]
    ] = []

    for metric_name in metric_names:
        datapoints = (
            metric_datapoints[
                metric_name
            ]
        )

        datapoints.sort(
            key=lambda item: (
                item["timestamp"]
            )
        )

        metric_definition = (
            METRIC_CONFIG[
                metric_name
            ]
        )

        payloads.append(
            {
                "schema_version": "1.0",
                "source": (
                    "Amazon CloudWatch"
                ),
                "namespace": "AWS/RDS",
                "region": region_name,
                "resource_type": (
                    RESOURCE_DIMENSIONS[
                        resource_dimension
                    ]
                ),
                "resource_dimension": (
                    resource_dimension
                ),
                "resource_id": (
                    resource_id
                ),
                "metric_name": (
                    metric_name
                ),
                "statistic": (
                    metric_definition[
                        "statistic"
                    ]
                ),
                "unit": (
                    metric_definition[
                        "unit"
                    ]
                ),
                "period_seconds": (
                    period_seconds
                ),
                "start_time": (
                    start_time
                    .isoformat()
                    .replace(
                        "+00:00",
                        "Z",
                    )
                ),
                "end_time": (
                    end_time
                    .isoformat()
                    .replace(
                        "+00:00",
                        "Z",
                    )
                ),
                "collected_at": (
                    collected_at
                    .isoformat()
                    .replace(
                        "+00:00",
                        "Z",
                    )
                ),
                "datapoint_count": (
                    len(datapoints)
                ),
                "datapoints": (
                    datapoints
                ),
            }
        )

    return payloads


def save_json(
    payload: dict[str, Any],
    output_root: Path = Path(
        "data/raw/cloudwatch"
    ),
) -> Path:
    """Save metric data using partitioned directories."""

    end_time = datetime.fromisoformat(
        payload["end_time"].replace(
            "Z",
            "+00:00",
        )
    )

    output_path = (
        output_root
        / f"region={payload['region']}"
        / (
            f"metric="
            f"{payload['metric_name']}"
        )
        / f"date={end_time:%Y-%m-%d}"
        / (
            f"{payload['resource_id']}_"
            f"{end_time:%Y%m%dT%H%M%SZ}"
            ".json"
        )
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with output_path.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            payload,
            file,
            ensure_ascii=False,
            indent=2,
        )

    return output_path


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Collect Amazon RDS "
            "CloudWatch metrics."
        )
    )

    parser.add_argument(
        "--resource-id",
        "--db-instance-identifier",
        dest="resource_id",
        required=True,
        help=(
            "Amazon RDS DB instance "
            "or Aurora cluster identifier"
        ),
    )

    parser.add_argument(
        "--resource-dimension",
        choices=list(
            RESOURCE_DIMENSIONS
        ),
        default=(
            "DBInstanceIdentifier"
        ),
        help=(
            "CloudWatch resource dimension"
        ),
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
        "--metrics",
        nargs="+",
        choices=list(
            METRIC_CONFIG
        ),
        default=list(
            METRIC_CONFIG
        ),
        help=(
            "CloudWatch metrics to collect"
        ),
    )

    parser.add_argument(
        "--lookback-minutes",
        type=int,
        default=60,
        help=(
            "Metric lookback period "
            "in minutes"
        ),
    )

    parser.add_argument(
        "--period-seconds",
        type=int,
        default=300,
        help=(
            "CloudWatch aggregation "
            "period in seconds"
        ),
    )

    return parser.parse_args()


def main() -> None:
    """Run the CloudWatch metric collector."""

    args = parse_arguments()

    try:
        session = create_session(
            profile_name=args.profile,
            region_name=args.region,
        )

        payloads = collect_metrics(
            session=session,
            region_name=args.region,
            resource_dimension=(
                args.resource_dimension
            ),
            resource_id=(
                args.resource_id
            ),
            metric_names=args.metrics,
            lookback_minutes=(
                args.lookback_minutes
            ),
            period_seconds=(
                args.period_seconds
            ),
        )

        for payload in payloads:
            output_path = save_json(
                payload
            )

            print(
                f"Metric: "
                f"{payload['metric_name']}"
            )

            print(
                f"Datapoints: "
                f"{payload['datapoint_count']}"
            )

            print(
                f"Output: {output_path}"
            )

            print("-" * 60)

    except ProfileNotFound as error:
        raise SystemExit(
            f"AWS profile not found: "
            f"{error}"
        ) from error

    except ValueError as error:
        raise SystemExit(
            str(error)
        ) from error

    except (
        ClientError,
        BotoCoreError,
    ) as error:
        raise SystemExit(
            "AWS API request failed: "
            f"{error}"
        ) from error


if __name__ == "__main__":
    main()