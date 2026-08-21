"""Tests for src.query.top_sql (no AWS calls)."""

from src.query.top_sql import (
    _truncate_sql_text,
    resolve_pi_dbi_resource_ids,
)


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
    assert (
        _truncate_sql_text("SELECT 1")
        == "SELECT 1"
    )


def test_truncate_sql_text_caps_long_statements():
    long_sql = "SELECT * FROM t WHERE " + (
        "x " * 400
    )

    truncated = _truncate_sql_text(long_sql)

    assert len(truncated) == 503  # 500 + "..."
    assert truncated.endswith("...")


def test_truncate_sql_text_passes_through_none():
    assert _truncate_sql_text(None) is None
