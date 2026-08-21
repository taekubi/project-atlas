"""Tests for src.query.top_sql (no AWS calls)."""

from unittest.mock import MagicMock

import pytest

from src.query.top_sql import (
    _SQL_TEXT_CHAR_LIMIT,
    _truncate_sql_text,
    fetch_top_sql,
    resolve_pi_dbi_resource_ids,
)


def _pi_session_returning(keys):
    """Build a session whose pi client returns `keys` from PI."""

    pi = MagicMock()
    pi.describe_dimension_keys.return_value = {
        "Keys": keys
    }

    session = MagicMock()
    session.client.return_value = pi

    return session, pi


def _instance(identifier, pi_enabled, dbi_resource_id=None):
    return {
        "identifier": identifier,
        "performance_insights_enabled": pi_enabled,
        "dbi_resource_id": dbi_resource_id,
    }


def test_resolve_pi_dbi_resource_ids_splits_by_pi_enabled():
    inventory = {
        "instances": [
            _instance(
                "watchcon-a", True, "db-ABC123"
            ),
            _instance("rds-devops", False),
        ]
    }

    mapping, without_pi = (
        resolve_pi_dbi_resource_ids(
            ["watchcon-a", "rds-devops"],
            inventory,
        )
    )

    assert mapping == {
        "watchcon-a": "db-ABC123"
    }
    assert without_pi == ["rds-devops"]


def test_resolve_pi_dbi_resource_ids_treats_unknown_resource_as_no_pi():
    inventory = {"instances": []}

    mapping, without_pi = (
        resolve_pi_dbi_resource_ids(
            ["unknown-instance"], inventory
        )
    )

    assert mapping == {}
    assert without_pi == ["unknown-instance"]


def test_resolve_pi_dbi_resource_ids_treats_missing_dbi_id_as_no_pi():
    # performance_insights_enabled True but dbi_resource_id somehow
    # missing -- should still be treated as unusable, not crash.
    inventory = {
        "instances": [
            _instance("watchcon-a", True, None)
        ]
    }

    mapping, without_pi = (
        resolve_pi_dbi_resource_ids(
            ["watchcon-a"], inventory
        )
    )

    assert mapping == {}
    assert without_pi == ["watchcon-a"]


def test_truncate_sql_text_leaves_short_text_unchanged():
    assert _truncate_sql_text(
        "SELECT 1"
    ) == ("SELECT 1", False)


def test_truncate_sql_text_caps_long_statements():
    long_sql = "SELECT * FROM t WHERE " + (
        "x " * 2000
    )

    truncated, was_truncated = (
        _truncate_sql_text(long_sql)
    )

    assert was_truncated is True
    assert (
        len(truncated)
        == _SQL_TEXT_CHAR_LIMIT + 1
    )
    assert truncated.endswith("…")


def test_truncate_sql_text_reports_no_truncation_at_exactly_the_limit():
    # An off-by-one here would mark a complete statement as cut off and
    # push the model to hedge a finding it could have stated plainly.
    exact = "x" * _SQL_TEXT_CHAR_LIMIT

    assert _truncate_sql_text(exact) == (
        exact,
        False,
    )


def test_truncate_sql_text_passes_through_none():
    assert _truncate_sql_text(None) == (
        None,
        False,
    )


def test_fetch_top_sql_flags_a_truncated_statement():
    session, _ = _pi_session_returning(
        [
            {
                "Dimensions": {
                    "db.sql.tokenized_id": "abc",
                    "db.sql.statement": "SELECT "
                    + ("x " * 2000),
                },
                "Total": 1.5,
            }
        ]
    )

    rows = fetch_top_sql(
        session=session,
        dbi_resource_id="db-ABC123",
        lookback_minutes=10,
    )

    assert rows[0]["sql_text_truncated"] is True
    assert rows[0]["sql_text"].endswith("…")


def test_fetch_top_sql_does_not_flag_a_short_statement():
    session, _ = _pi_session_returning(
        [
            {
                "Dimensions": {
                    "db.sql.tokenized_id": "abc",
                    "db.sql.statement": "select * from alert",
                },
                "Total": 0.2444,
            }
        ]
    )

    rows = fetch_top_sql(
        session=session,
        dbi_resource_id="db-ABC123",
        lookback_minutes=10,
    )

    assert (
        rows[0]["sql_text_truncated"] is False
    )
    assert (
        rows[0]["sql_text"]
        == "select * from alert"
    )
    assert (
        rows[0]["avg_active_sessions"] == 0.2444
    )


def test_fetch_top_sql_handles_a_missing_statement_dimension():
    session, _ = _pi_session_returning(
        [
            {
                "Dimensions": {
                    "db.sql.tokenized_id": "abc"
                },
                "Total": 0.5,
            }
        ]
    )

    rows = fetch_top_sql(
        session=session,
        dbi_resource_id="db-ABC123",
        lookback_minutes=10,
    )

    assert rows[0]["sql_text"] is None
    assert (
        rows[0]["sql_text_truncated"] is False
    )


def test_fetch_top_sql_rejects_an_invalid_dbi_resource_id():
    session, _ = _pi_session_returning([])

    with pytest.raises(ValueError):
        fetch_top_sql(
            session=session,
            dbi_resource_id="not-a-dbi-id",
            lookback_minutes=10,
        )


def test_fetch_top_sql_rejects_an_out_of_range_lookback():
    session, _ = _pi_session_returning([])

    with pytest.raises(ValueError):
        fetch_top_sql(
            session=session,
            dbi_resource_id="db-ABC123",
            lookback_minutes=0,
        )


def test_fetch_top_sql_queries_the_db_load_metric_grouped_by_sql():
    session, pi = _pi_session_returning([])

    fetch_top_sql(
        session=session,
        dbi_resource_id="db-ABC123",
        lookback_minutes=10,
        max_results=5,
    )

    call = pi.describe_dimension_keys.call_args.kwargs
    assert call["ServiceType"] == "RDS"
    assert call["Identifier"] == "db-ABC123"
    assert call["Metric"] == "db.load.avg"
    assert call["GroupBy"]["Group"] == "db.sql"
    assert call["MaxResults"] == 5
