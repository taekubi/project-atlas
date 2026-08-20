"""Transform Project Atlas raw CloudWatch JSON into curated Parquet."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


CURATED_SCHEMA = pa.schema(
    [
        pa.field(
            "target_name",
            pa.string(),
        ),
        pa.field(
            "resource_id",
            pa.string(),
            nullable=False,
        ),
        pa.field(
            "engine",
            pa.string(),
        ),
        pa.field(
            "cluster_identifier",
            pa.string(),
        ),
        pa.field(
            "cluster_role",
            pa.string(),
        ),
        pa.field(
            "metric_profile",
            pa.string(),
        ),
        pa.field(
            "resource_profile",
            pa.string(),
        ),
        pa.field(
            "metric_name",
            pa.string(),
            nullable=False,
        ),
        pa.field(
            "statistic",
            pa.string(),
        ),
        pa.field(
            "unit",
            pa.string(),
        ),
        pa.field(
            "metric_timestamp",
            pa.timestamp("us"),
            nullable=False,
        ),
        pa.field(
            "value",
            pa.float64(),
            nullable=False,
        ),
        pa.field(
            "period_seconds",
            pa.int32(),
        ),
        pa.field(
            "collected_at",
            pa.timestamp("us"),
            nullable=False,
        ),
        pa.field(
            "source_file",
            pa.string(),
            nullable=False,
        ),
    ]
)


def _parse_utc_timestamp(
    value: str,
) -> datetime:
    """Parse an ISO timestamp and return UTC as naive datetime."""

    parsed = datetime.fromisoformat(
        value.replace(
            "Z",
            "+00:00",
        )
    )

    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=timezone.utc
        )

    return (
        parsed
        .astimezone(
            timezone.utc
        )
        .replace(
            tzinfo=None
        )
    )


def _partition_value(
    path: Path,
    prefix: str,
) -> str | None:
    """Read a Hive-style partition value from a path."""

    marker = f"{prefix}="

    for part in path.parts:
        if part.startswith(
            marker
        ):
            return part.split(
                "=",
                1,
            )[1]

    return None


def _required_value(
    value: Any,
    field_name: str,
    source_file: Path,
) -> Any:
    """Return a required value or fail with source context."""

    if value is None:
        raise ValueError(
            f"Missing {field_name}: "
            f"{source_file}"
        )

    if (
        isinstance(
            value,
            str,
        )
        and not value.strip()
    ):
        raise ValueError(
            f"Empty {field_name}: "
            f"{source_file}"
        )

    return value


def flatten_raw_file(
    source_file: Path,
    input_root: Path,
) -> tuple[
    list[dict[str, Any]],
    bool,
]:
    """Flatten one raw Atlas metric JSON into metric rows."""

    with source_file.open(
        "r",
        encoding="utf-8",
    ) as file:
        payload = json.load(
            file
        )

    account_id = (
        payload.get(
            "source_account_id"
        )
        or _partition_value(
            source_file,
            "account_id",
        )
    )

    region = (
        payload.get(
            "region"
        )
        or _partition_value(
            source_file,
            "region",
        )
    )

    account_id = str(
        _required_value(
            account_id,
            "source_account_id",
            source_file,
        )
    )

    region = str(
        _required_value(
            region,
            "region",
            source_file,
        )
    )

    resource_id = str(
        _required_value(
            payload.get(
                "resource_id"
            ),
            "resource_id",
            source_file,
        )
    )

    metric_name = str(
        _required_value(
            payload.get(
                "metric_name"
            ),
            "metric_name",
            source_file,
        )
    )

    collected_at_raw = str(
        _required_value(
            payload.get(
                "collected_at"
            ),
            "collected_at",
            source_file,
        )
    )

    collected_at = (
        _parse_utc_timestamp(
            collected_at_raw
        )
    )

    try:
        source_name = str(
            source_file.relative_to(
                input_root
            )
        )
    except ValueError:
        source_name = str(
            source_file
        )

    datapoints = payload.get(
        "datapoints",
        [],
    )

    if not isinstance(
        datapoints,
        list,
    ):
        raise ValueError(
            "datapoints must be a list: "
            f"{source_file}"
        )

    rows: list[
        dict[str, Any]
    ] = []

    for datapoint in datapoints:
        timestamp_raw = (
            _required_value(
                datapoint.get(
                    "timestamp"
                ),
                "datapoint.timestamp",
                source_file,
            )
        )

        value_raw = (
            _required_value(
                datapoint.get(
                    "value"
                ),
                "datapoint.value",
                source_file,
            )
        )

        rows.append(
            {
                "_account_id": (
                    account_id
                ),
                "_region": (
                    region
                ),
                "target_name": (
                    payload.get(
                        "target_name"
                    )
                ),
                "resource_id": (
                    resource_id
                ),
                "engine": (
                    payload.get(
                        "engine"
                    )
                ),
                "cluster_identifier": (
                    payload.get(
                        "cluster_identifier"
                    )
                ),
                "cluster_role": (
                    payload.get(
                        "cluster_role"
                    )
                ),
                "metric_profile": (
                    payload.get(
                        "metric_profile"
                    )
                ),
                "resource_profile": (
                    payload.get(
                        "resource_profile"
                    )
                ),
                "metric_name": (
                    metric_name
                ),
                "statistic": (
                    payload.get(
                        "statistic"
                    )
                ),
                "unit": (
                    payload.get(
                        "unit"
                    )
                ),
                "metric_timestamp": (
                    _parse_utc_timestamp(
                        str(
                            timestamp_raw
                        )
                    )
                ),
                "value": float(
                    value_raw
                ),
                "period_seconds": (
                    payload.get(
                        "period_seconds"
                    )
                ),
                "collected_at": (
                    collected_at
                ),
                "source_file": (
                    source_name
                ),
            }
        )

    return (
        rows,
        len(datapoints) == 0,
    )


def deduplicate_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep the latest collection for each metric timestamp."""

    latest: dict[
        tuple[
            str,
            str,
            str,
            str,
            datetime,
        ],
        dict[str, Any],
    ] = {}

    for row in rows:
        key = (
            row["_account_id"],
            row["_region"],
            row["resource_id"],
            row["metric_name"],
            row["metric_timestamp"],
        )

        existing = latest.get(
            key
        )

        if (
            existing is None
            or row["collected_at"]
            > existing["collected_at"]
        ):
            latest[
                key
            ] = row

    deduplicated = list(
        latest.values()
    )

    deduplicated.sort(
        key=lambda row: (
            row["_account_id"],
            row["_region"],
            row["metric_timestamp"],
            row["resource_id"],
            row["metric_name"],
        )
    )

    return deduplicated


def write_curated_partitions(
    rows: list[dict[str, Any]],
    output_root: Path,
) -> list[Path]:
    """Write hourly Hive-style Parquet partitions."""

    partitions: dict[
        tuple[
            str,
            str,
            str,
            str,
        ],
        list[dict[str, Any]],
    ] = defaultdict(
        list
    )

    for row in rows:
        timestamp = row[
            "metric_timestamp"
        ]

        partition_key = (
            row["_account_id"],
            row["_region"],
            f"{timestamp:%Y-%m-%d}",
            f"{timestamp:%H}",
        )

        curated_row = {
            key: value
            for key, value in row.items()
            if not key.startswith(
                "_"
            )
        }

        partitions[
            partition_key
        ].append(
            curated_row
        )

    output_files: list[
        Path
    ] = []

    for (
        account_id,
        region,
        date_value,
        hour_value,
    ), partition_rows in sorted(
        partitions.items()
    ):
        partition_rows.sort(
            key=lambda row: (
                row[
                    "metric_timestamp"
                ],
                row[
                    "resource_id"
                ],
                row[
                    "metric_name"
                ],
            )
        )

        output_path = (
            output_root
            / (
                f"account_id="
                f"{account_id}"
            )
            / f"region={region}"
            / f"date={date_value}"
            / f"hour={hour_value}"
            / "metrics.parquet"
        )

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        table = (
            pa.Table.from_pylist(
                partition_rows,
                schema=CURATED_SCHEMA,
            )
        )

        pq.write_table(
            table,
            output_path,
            compression="snappy",
            use_dictionary=True,
            write_statistics=True,
        )

        output_files.append(
            output_path
        )

    return output_files


def transform_raw_to_curated(
    input_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Transform raw Atlas JSON files into curated Parquet."""

    source_files = sorted(
        input_root.rglob(
            "*.json"
        )
    )

    if not source_files:
        raise ValueError(
            "No raw JSON files found under "
            f"{input_root}"
        )

    all_rows: list[
        dict[str, Any]
    ] = []

    no_data_files = 0

    for source_file in source_files:
        rows, no_data = (
            flatten_raw_file(
                source_file=source_file,
                input_root=input_root,
            )
        )

        all_rows.extend(
            rows
        )

        if no_data:
            no_data_files += 1

    deduplicated_rows = (
        deduplicate_rows(
            all_rows
        )
    )

    output_files = (
        write_curated_partitions(
            rows=deduplicated_rows,
            output_root=output_root,
        )
    )

    total_parquet_bytes = sum(
        path.stat().st_size
        for path in output_files
    )

    return {
        "raw_file_count": (
            len(source_files)
        ),
        "no_data_file_count": (
            no_data_files
        ),
        "raw_datapoint_count": (
            len(all_rows)
        ),
        "curated_row_count": (
            len(
                deduplicated_rows
            )
        ),
        "duplicates_removed": (
            len(all_rows)
            - len(
                deduplicated_rows
            )
        ),
        "partition_count": (
            len(output_files)
        ),
        "parquet_file_count": (
            len(output_files)
        ),
        "parquet_total_bytes": (
            total_parquet_bytes
        ),
        "output_files": (
            output_files
        ),
    }


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Transform Project Atlas "
            "CloudWatch raw JSON "
            "into hourly Parquet."
        )
    )

    parser.add_argument(
        "--input-root",
        required=True,
        help=(
            "Local root containing "
            "raw CloudWatch JSON files"
        ),
    )

    parser.add_argument(
        "--output-root",
        required=True,
        help=(
            "Local root for curated "
            "Parquet output"
        ),
    )

    return parser.parse_args()


def main() -> None:
    """Run the raw-to-curated transformation."""

    args = parse_arguments()

    input_root = Path(
        args.input_root
    )

    output_root = Path(
        args.output_root
    )

    result = transform_raw_to_curated(
        input_root=input_root,
        output_root=output_root,
    )

    print()
    print("=" * 60)
    print(
        "PROJECT ATLAS "
        "RAW -> CURATED TRANSFORM"
    )
    print("=" * 60)

    print(
        "Raw JSON files       : "
        f"{result['raw_file_count']}"
    )

    print(
        "No-data JSON files   : "
        f"{result['no_data_file_count']}"
    )

    print(
        "Raw datapoints       : "
        f"{result['raw_datapoint_count']}"
    )

    print(
        "Curated rows         : "
        f"{result['curated_row_count']}"
    )

    print(
        "Duplicates removed   : "
        f"{result['duplicates_removed']}"
    )

    print(
        "Parquet partitions   : "
        f"{result['partition_count']}"
    )

    print(
        "Parquet files        : "
        f"{result['parquet_file_count']}"
    )

    print(
        "Parquet total bytes  : "
        f"{result['parquet_total_bytes']}"
    )

    print()

    for path in result[
        "output_files"
    ]:
        print(
            f"  - {path}"
        )


if __name__ == "__main__":
    main()