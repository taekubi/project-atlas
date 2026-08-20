"""Tests for src.query.db_health query building (no AWS calls)."""

import pytest

from src.query.db_health import build_db_health_query


def test_build_query_without_resource_filter_covers_whole_account():
    query = build_db_health_query(
        account_id="826846563965",
        region="ap-northeast-2",
        date="2026-08-19",
    )

    assert "account_id = '826846563965'" in query
    assert "region = 'ap-northeast-2'" in query
    assert "date = '2026-08-19'" in query
    assert "resource_id IN" not in query
    assert "resource_id =" not in query


def test_build_query_with_one_resource_id_filters_it():
    query = build_db_health_query(
        account_id="826846563965",
        region="ap-northeast-2",
        date="2026-08-19",
        resource_ids=["watchcon-a"],
    )

    assert "resource_id IN ('watchcon-a')" in query


def test_build_query_with_multiple_resource_ids_uses_in_clause():
    query = build_db_health_query(
        account_id="826846563965",
        region="ap-northeast-2",
        date="2026-08-19",
        resource_ids=["watchcon-a", "watchcon-c"],
    )

    assert (
        "resource_id IN ('watchcon-a', 'watchcon-c')" in query
    )


def test_build_query_rejects_invalid_account_id():
    with pytest.raises(ValueError):
        build_db_health_query(
            account_id="not-an-account",
            region="ap-northeast-2",
            date="2026-08-19",
        )


def test_build_query_rejects_invalid_resource_id_in_list():
    with pytest.raises(ValueError):
        build_db_health_query(
            account_id="826846563965",
            region="ap-northeast-2",
            date="2026-08-19",
            resource_ids=["watchcon-a", "bad;drop table"],
        )
