"""Tests for src.query.athena_client result handling (no AWS calls)."""

from unittest.mock import MagicMock

import pytest

from src.query.athena_client import (
    AthenaQueryError,
    _fetch_rows,
    _wait_for_completion,
    format_table,
)


def _client_returning(pages: list[dict]) -> MagicMock:
    """Build a client whose get_query_results paginator yields `pages`."""

    paginator = MagicMock()
    paginator.paginate.return_value = iter(pages)

    client = MagicMock()
    client.get_paginator.return_value = paginator

    return client


def _page(rows: list[list[str | None]]) -> dict:
    return {
        "ResultSet": {
            "Rows": [
                {
                    "Data": [
                        {}
                        if value is None
                        else {"VarCharValue": value}
                        for value in row
                    ]
                }
                for row in rows
            ]
        }
    }


def test_fetch_rows_maps_the_header_onto_values():
    client = _client_returning(
        [
            _page(
                [
                    ["resource_id", "cpu_avg"],
                    ["watchcon-a", "42.5"],
                ]
            )
        ]
    )

    assert _fetch_rows(client, "q-1") == [
        {
            "resource_id": "watchcon-a",
            "cpu_avg": "42.5",
        }
    ]


def test_fetch_rows_survives_an_empty_result_set():
    # MSCK REPAIR TABLE returns no rows at all when there is nothing to
    # repair. Reading a header out of that list used to raise
    # IndexError, which failed the hourly curated refresh on every hour
    # that brought no new partitions.
    client = _client_returning([_page([])])

    assert _fetch_rows(client, "q-1") == []


def test_fetch_rows_skips_an_empty_page_between_full_ones():
    client = _client_returning(
        [
            _page([]),
            _page(
                [
                    ["resource_id"],
                    ["watchcon-a"],
                ]
            ),
        ]
    )

    assert _fetch_rows(client, "q-1") == [
        {"resource_id": "watchcon-a"}
    ]


def test_fetch_rows_reads_the_header_only_once_across_pages():
    # Only the first page carries the header row; a later page's first
    # row is data and must not be swallowed as a header.
    client = _client_returning(
        [
            _page(
                [
                    ["resource_id"],
                    ["watchcon-a"],
                ]
            ),
            _page([["watchcon-c"]]),
        ]
    )

    assert _fetch_rows(client, "q-1") == [
        {"resource_id": "watchcon-a"},
        {"resource_id": "watchcon-c"},
    ]


def test_fetch_rows_keeps_a_null_cell_as_none():
    # A NULL in Athena arrives as a Data entry with no VarCharValue;
    # it must stay None rather than becoming an empty string, since
    # downstream code treats None as "not applicable".
    client = _client_returning(
        [
            _page(
                [
                    ["resource_id", "cpu_avg"],
                    ["watchcon-a", None],
                ]
            )
        ]
    )

    assert _fetch_rows(client, "q-1") == [
        {
            "resource_id": "watchcon-a",
            "cpu_avg": None,
        }
    ]


def test_wait_for_completion_returns_on_success():
    client = MagicMock()
    client.get_query_execution.return_value = {
        "QueryExecution": {
            "Status": {"State": "SUCCEEDED"}
        }
    }

    _wait_for_completion(
        client=client,
        query_execution_id="q-1",
        poll_seconds=0,
        timeout_seconds=1,
    )


def test_wait_for_completion_reports_the_failure_reason():
    client = MagicMock()
    client.get_query_execution.return_value = {
        "QueryExecution": {
            "Status": {
                "State": "FAILED",
                "StateChangeReason": (
                    "TABLE_NOT_FOUND"
                ),
            }
        }
    }

    with pytest.raises(
        AthenaQueryError,
        match="TABLE_NOT_FOUND",
    ):
        _wait_for_completion(
            client=client,
            query_execution_id="q-1",
            poll_seconds=0,
            timeout_seconds=1,
        )


def test_wait_for_completion_times_out_on_a_stuck_query():
    client = MagicMock()
    client.get_query_execution.return_value = {
        "QueryExecution": {
            "Status": {"State": "RUNNING"}
        }
    }

    with pytest.raises(
        AthenaQueryError,
        match="timed out",
    ):
        _wait_for_completion(
            client=client,
            query_execution_id="q-1",
            poll_seconds=0,
            timeout_seconds=0,
        )


def test_format_table_reports_no_rows():
    assert format_table([]) == "(no rows)"


def test_format_table_renders_a_null_cell_as_blank():
    text = format_table(
        [
            {
                "resource_id": "watchcon-a",
                "cpu_avg": None,
            }
        ]
    )

    assert "watchcon-a" in text
    assert "None" not in text
