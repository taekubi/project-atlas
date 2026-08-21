"""Tests for src.transforms.cloudwatch_curated (real pyarrow, temp dirs)."""

import json
from datetime import datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from src.transforms.cloudwatch_curated import (
    _parse_utc_timestamp,
    _partition_value,
    _required_value,
    deduplicate_rows,
    flatten_raw_file,
    transform_raw_to_curated,
    write_curated_partitions,
)

_BASE_PAYLOAD = {
    "source_account_id": "826846563965",
    "region": "ap-northeast-2",
    "target_name": "headquarters",
    "resource_id": "watchcon-a",
    "engine": "aurora-postgresql",
    "cluster_identifier": "watchcon-cluster",
    "cluster_role": "writer",
    "metric_profile": "operational-v1",
    "resource_profile": "aurora-writer",
    "metric_name": "CPUUtilization",
    "statistic": "Average",
    "unit": "Percent",
    "period_seconds": 300,
    "collected_at": "2026-08-19T07:00:05Z",
    "datapoints": [
        {
            "timestamp": "2026-08-19T06:55:00Z",
            "value": 42.5,
        }
    ],
}


def _write_raw_file(
    root: Path,
    relative_path: str,
    payload: dict,
) -> Path:
    path = root / relative_path
    path.parent.mkdir(
        parents=True, exist_ok=True
    )
    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return path


# --- _parse_utc_timestamp ----------------------------------------------


def test_parse_utc_timestamp_handles_z_suffix():
    parsed = _parse_utc_timestamp(
        "2026-08-19T06:55:00Z"
    )

    assert parsed == datetime(
        2026, 8, 19, 6, 55, 0
    )


def test_parse_utc_timestamp_converts_a_non_utc_offset():
    # KST (+09:00) 15:55 is 06:55 UTC -- the stored value must be UTC
    # regardless of what offset the source used, since every row in one
    # partition needs a comparable timestamp.
    parsed = _parse_utc_timestamp(
        "2026-08-19T15:55:00+09:00"
    )

    assert parsed == datetime(
        2026, 8, 19, 6, 55, 0
    )


def test_parse_utc_timestamp_treats_naive_input_as_already_utc():
    parsed = _parse_utc_timestamp(
        "2026-08-19T06:55:00"
    )

    assert parsed == datetime(
        2026, 8, 19, 6, 55, 0
    )


def test_parse_utc_timestamp_returns_a_naive_datetime():
    # Parquet's timestamp("us") column and the partition-key grouping
    # in write_curated_partitions both assume naive values throughout;
    # a stray tz-aware datetime would break comparisons silently.
    parsed = _parse_utc_timestamp(
        "2026-08-19T06:55:00Z"
    )

    assert parsed.tzinfo is None


# --- _partition_value ----------------------------------------------------


def test_partition_value_reads_a_hive_segment():
    path = Path(
        "raw/cloudwatch/account_id=826846563965/"
        "region=ap-northeast-2/x.json"
    )

    assert (
        _partition_value(path, "account_id")
        == "826846563965"
    )
    assert (
        _partition_value(path, "region")
        == "ap-northeast-2"
    )


def test_partition_value_returns_none_when_absent():
    path = Path("raw/cloudwatch/x.json")

    assert (
        _partition_value(path, "account_id")
        is None
    )


# --- _required_value -------------------------------------------------


def test_required_value_passes_through_a_present_value():
    assert (
        _required_value(
            "watchcon-a",
            "resource_id",
            Path("x.json"),
        )
        == "watchcon-a"
    )


def test_required_value_rejects_none():
    with pytest.raises(
        ValueError, match="resource_id"
    ):
        _required_value(
            None,
            "resource_id",
            Path("x.json"),
        )


def test_required_value_rejects_a_blank_string():
    with pytest.raises(
        ValueError, match="resource_id"
    ):
        _required_value(
            "   ",
            "resource_id",
            Path("x.json"),
        )


def test_required_value_accepts_zero():
    # 0 is falsy but a legitimate metric value; only None/blank-string
    # must be rejected, not anything Python considers falsy.
    assert (
        _required_value(
            0, "value", Path("x.json")
        )
        == 0
    )


# --- flatten_raw_file --------------------------------------------------


def test_flatten_raw_file_reads_the_happy_path(tmp_path):
    source = _write_raw_file(
        tmp_path,
        "x.json",
        _BASE_PAYLOAD,
    )

    rows, no_data = flatten_raw_file(
        source_file=source,
        input_root=tmp_path,
    )

    assert no_data is False
    assert len(rows) == 1
    row = rows[0]
    assert row["resource_id"] == "watchcon-a"
    assert row["metric_name"] == "CPUUtilization"
    assert row["value"] == 42.5
    assert row["_account_id"] == "826846563965"
    assert row["_region"] == "ap-northeast-2"
    assert row["metric_timestamp"] == datetime(
        2026, 8, 19, 6, 55, 0
    )
    assert row["source_file"] == "x.json"


def test_flatten_raw_file_reports_no_data_for_an_empty_datapoints_list(
    tmp_path,
):
    payload = {**_BASE_PAYLOAD, "datapoints": []}
    source = _write_raw_file(
        tmp_path, "raw/x.json", payload
    )

    rows, no_data = flatten_raw_file(
        source_file=source,
        input_root=tmp_path,
    )

    assert rows == []
    assert no_data is True


def test_flatten_raw_file_falls_back_to_the_path_partition_for_account_and_region(
    tmp_path,
):
    # Raw objects written before account_id partitioning are missing
    # source_account_id/region in the payload; the Hive path segments
    # are the only source left for those.
    payload = {
        k: v
        for k, v in _BASE_PAYLOAD.items()
        if k not in ("source_account_id", "region")
    }
    source = _write_raw_file(
        tmp_path,
        "raw/account_id=826846563965/region=ap-northeast-2/x.json",
        payload,
    )

    rows, _ = flatten_raw_file(
        source_file=source,
        input_root=tmp_path,
    )

    assert rows[0]["_account_id"] == "826846563965"
    assert rows[0]["_region"] == "ap-northeast-2"


def test_flatten_raw_file_prefers_the_payload_over_the_path_partition(
    tmp_path,
):
    source = _write_raw_file(
        tmp_path,
        "raw/account_id=000000000000/region=us-east-1/x.json",
        _BASE_PAYLOAD,
    )

    rows, _ = flatten_raw_file(
        source_file=source,
        input_root=tmp_path,
    )

    assert rows[0]["_account_id"] == "826846563965"
    assert rows[0]["_region"] == "ap-northeast-2"


def test_flatten_raw_file_raises_when_account_id_is_unavailable_anywhere(
    tmp_path,
):
    payload = {
        k: v
        for k, v in _BASE_PAYLOAD.items()
        if k not in ("source_account_id", "region")
    }
    source = _write_raw_file(
        tmp_path, "raw/x.json", payload
    )

    with pytest.raises(
        ValueError,
        match="source_account_id",
    ):
        flatten_raw_file(
            source_file=source,
            input_root=tmp_path,
        )


def test_flatten_raw_file_raises_on_a_missing_datapoint_value(tmp_path):
    payload = {
        **_BASE_PAYLOAD,
        "datapoints": [
            {"timestamp": "2026-08-19T06:55:00Z"}
        ],
    }
    source = _write_raw_file(
        tmp_path, "raw/x.json", payload
    )

    with pytest.raises(
        ValueError, match="datapoint.value"
    ):
        flatten_raw_file(
            source_file=source,
            input_root=tmp_path,
        )


def test_flatten_raw_file_rejects_datapoints_that_are_not_a_list(
    tmp_path,
):
    payload = {
        **_BASE_PAYLOAD,
        "datapoints": "not-a-list",
    }
    source = _write_raw_file(
        tmp_path, "raw/x.json", payload
    )

    with pytest.raises(
        ValueError, match="datapoints"
    ):
        flatten_raw_file(
            source_file=source,
            input_root=tmp_path,
        )


def test_flatten_raw_file_flattens_multiple_datapoints_into_multiple_rows(
    tmp_path,
):
    payload = {
        **_BASE_PAYLOAD,
        "datapoints": [
            {
                "timestamp": "2026-08-19T06:50:00Z",
                "value": 40.0,
            },
            {
                "timestamp": "2026-08-19T06:55:00Z",
                "value": 42.5,
            },
        ],
    }
    source = _write_raw_file(
        tmp_path, "raw/x.json", payload
    )

    rows, _ = flatten_raw_file(
        source_file=source,
        input_root=tmp_path,
    )

    assert [row["value"] for row in rows] == [
        40.0,
        42.5,
    ]


# --- deduplicate_rows ----------------------------------------------------


def _row(**overrides) -> dict:
    row = {
        "_account_id": "826846563965",
        "_region": "ap-northeast-2",
        "resource_id": "watchcon-a",
        "metric_name": "CPUUtilization",
        "metric_timestamp": datetime(
            2026, 8, 19, 6, 55, 0
        ),
        "collected_at": datetime(
            2026, 8, 19, 7, 0, 5
        ),
        "value": 42.5,
    }
    row.update(overrides)
    return row


def test_deduplicate_rows_keeps_the_most_recently_collected_row():
    older = _row(
        value=40.0,
        collected_at=datetime(
            2026, 8, 19, 6, 59, 0
        ),
    )
    newer = _row(
        value=42.5,
        collected_at=datetime(
            2026, 8, 19, 7, 0, 5
        ),
    )

    deduplicated = deduplicate_rows(
        [older, newer]
    )

    assert len(deduplicated) == 1
    assert deduplicated[0]["value"] == 42.5


def test_deduplicate_rows_treats_a_different_resource_as_distinct():
    a = _row(resource_id="watchcon-a")
    c = _row(resource_id="watchcon-c")

    deduplicated = deduplicate_rows([a, c])

    assert len(deduplicated) == 2


def test_deduplicate_rows_treats_a_different_metric_timestamp_as_distinct():
    first = _row(
        metric_timestamp=datetime(
            2026, 8, 19, 6, 50, 0
        )
    )
    second = _row(
        metric_timestamp=datetime(
            2026, 8, 19, 6, 55, 0
        )
    )

    deduplicated = deduplicate_rows(
        [first, second]
    )

    assert len(deduplicated) == 2


def test_deduplicate_rows_sorts_output_deterministically():
    rows = [
        _row(
            resource_id="watchcon-c",
            metric_timestamp=datetime(
                2026, 8, 19, 6, 55, 0
            ),
        ),
        _row(
            resource_id="watchcon-a",
            metric_timestamp=datetime(
                2026, 8, 19, 6, 50, 0
            ),
        ),
        _row(
            resource_id="watchcon-a",
            metric_timestamp=datetime(
                2026, 8, 19, 6, 55, 0
            ),
        ),
    ]

    deduplicated = deduplicate_rows(rows)

    assert [
        (
            row["resource_id"],
            row["metric_timestamp"],
        )
        for row in deduplicated
    ] == [
        (
            "watchcon-a",
            datetime(2026, 8, 19, 6, 50, 0),
        ),
        (
            "watchcon-a",
            datetime(2026, 8, 19, 6, 55, 0),
        ),
        (
            "watchcon-c",
            datetime(2026, 8, 19, 6, 55, 0),
        ),
    ]


# --- write_curated_partitions (real pyarrow I/O) -----------------------


def _curated_row(**overrides) -> dict:
    row = {
        "_account_id": "826846563965",
        "_region": "ap-northeast-2",
        "target_name": "headquarters",
        "resource_id": "watchcon-a",
        "engine": "aurora-postgresql",
        "cluster_identifier": "watchcon-cluster",
        "cluster_role": "writer",
        "metric_profile": "operational-v1",
        "resource_profile": "aurora-writer",
        "metric_name": "CPUUtilization",
        "statistic": "Average",
        "unit": "Percent",
        "metric_timestamp": datetime(
            2026, 8, 19, 6, 55, 0
        ),
        "value": 42.5,
        "period_seconds": 300,
        "collected_at": datetime(
            2026, 8, 19, 7, 0, 5
        ),
        "source_file": "x.json",
    }
    row.update(overrides)
    return row


def test_write_curated_partitions_writes_one_file_per_account_region_date_hour(
    tmp_path,
):
    rows = [
        _curated_row(),
        _curated_row(
            metric_timestamp=datetime(
                2026, 8, 19, 8, 10, 0
            )
        ),
    ]

    output_files = write_curated_partitions(
        rows=rows, output_root=tmp_path
    )

    assert len(output_files) == 2
    relative = sorted(
        str(p.relative_to(tmp_path)).replace(
            "\\", "/"
        )
        for p in output_files
    )
    assert relative == [
        "account_id=826846563965/region=ap-northeast-2/"
        "date=2026-08-19/hour=06/metrics.parquet",
        "account_id=826846563965/region=ap-northeast-2/"
        "date=2026-08-19/hour=08/metrics.parquet",
    ]


def test_write_curated_partitions_strips_the_internal_grouping_columns(
    tmp_path,
):
    output_files = write_curated_partitions(
        rows=[_curated_row()],
        output_root=tmp_path,
    )

    table = pq.read_table(output_files[0])

    assert "_account_id" not in table.column_names
    assert "_region" not in table.column_names
    assert "resource_id" in table.column_names


def test_write_curated_partitions_output_is_readable_and_matches_input(
    tmp_path,
):
    output_files = write_curated_partitions(
        rows=[_curated_row(value=91.5)],
        output_root=tmp_path,
    )

    table = pq.read_table(output_files[0])
    row = table.to_pylist()[0]

    assert row["resource_id"] == "watchcon-a"
    assert row["value"] == 91.5
    assert row["metric_name"] == "CPUUtilization"


def test_write_curated_partitions_groups_two_resources_into_one_hourly_file(
    tmp_path,
):
    rows = [
        _curated_row(resource_id="watchcon-a"),
        _curated_row(resource_id="watchcon-c"),
    ]

    output_files = write_curated_partitions(
        rows=rows, output_root=tmp_path
    )

    assert len(output_files) == 1
    table = pq.read_table(output_files[0])
    assert sorted(table.column("resource_id").to_pylist()) == [
        "watchcon-a",
        "watchcon-c",
    ]


# --- transform_raw_to_curated (end to end, real files) -----------------


def test_transform_raw_to_curated_end_to_end(tmp_path):
    input_root = tmp_path / "raw"
    output_root = tmp_path / "curated"

    _write_raw_file(
        input_root,
        "a.json",
        _BASE_PAYLOAD,
    )
    _write_raw_file(
        input_root,
        "b.json",
        {
            **_BASE_PAYLOAD,
            "resource_id": "watchcon-c",
            "datapoints": [],
        },
    )

    result = transform_raw_to_curated(
        input_root=input_root,
        output_root=output_root,
    )

    assert result["raw_file_count"] == 2
    assert result["no_data_file_count"] == 1
    assert result["raw_datapoint_count"] == 1
    assert result["curated_row_count"] == 1
    assert result["duplicates_removed"] == 0
    assert result["parquet_file_count"] == 1
    assert result["parquet_total_bytes"] > 0

    table = pq.read_table(
        result["output_files"][0]
    )
    assert table.column(
        "resource_id"
    ).to_pylist() == ["watchcon-a"]


def test_transform_raw_to_curated_deduplicates_across_files(tmp_path):
    # Two Raw objects covering the same metric/timestamp (a retried
    # collection, or a rerun of this same transform) must collapse to
    # one curated row -- otherwise every rerun inflates the row count.
    input_root = tmp_path / "raw"
    output_root = tmp_path / "curated"

    _write_raw_file(
        input_root,
        "a.json",
        {
            **_BASE_PAYLOAD,
            "collected_at": "2026-08-19T07:00:00Z",
        },
    )
    _write_raw_file(
        input_root,
        "b.json",
        {
            **_BASE_PAYLOAD,
            "collected_at": "2026-08-19T07:05:00Z",
        },
    )

    result = transform_raw_to_curated(
        input_root=input_root,
        output_root=output_root,
    )

    assert result["raw_datapoint_count"] == 2
    assert result["curated_row_count"] == 1
    assert result["duplicates_removed"] == 1


def test_transform_raw_to_curated_raises_when_input_root_is_empty(
    tmp_path,
):
    input_root = tmp_path / "raw"
    input_root.mkdir()

    with pytest.raises(
        ValueError, match="No raw JSON files"
    ):
        transform_raw_to_curated(
            input_root=input_root,
            output_root=tmp_path / "curated",
        )
