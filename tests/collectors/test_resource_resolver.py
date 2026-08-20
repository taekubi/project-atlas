"""Tests for src.collectors.resource_resolver (no AWS calls)."""

import pytest

from src.collectors.resource_resolver import (
    ResourceResolutionError,
    resolve_resource_ids,
)

_INVENTORY = {
    "clusters": [
        {
            "identifier": "watchcon-cluster-cluster",
            "members": [
                {"identifier": "watchcon-a", "role": "writer"},
                {"identifier": "watchcon-c", "role": "reader"},
            ],
        },
        {
            "identifier": "rds-devops-cluster",
            "members": [
                {"identifier": "rds-devops", "role": "writer"},
            ],
        },
    ],
    "instances": [
        {"identifier": "watchcon-a"},
        {"identifier": "watchcon-c"},
        {"identifier": "rds-devops"},
        {"identifier": "woongjin-watch-slack-postgresql"},
    ],
}


def test_exact_instance_match_returns_only_that_instance():
    assert resolve_resource_ids(
        "watchcon-a", _INVENTORY
    ) == ["watchcon-a"]


def test_exact_cluster_match_returns_every_member():
    result = resolve_resource_ids(
        "watchcon-cluster-cluster", _INVENTORY
    )
    assert sorted(result) == ["watchcon-a", "watchcon-c"]


def test_substring_cluster_match_is_case_insensitive():
    result = resolve_resource_ids("WatchCon", _INVENTORY)
    assert sorted(result) == ["watchcon-a", "watchcon-c"]


def test_substring_instance_match_when_no_cluster_matches():
    result = resolve_resource_ids("devops", _INVENTORY)
    assert result == ["rds-devops"]


def test_no_match_raises():
    with pytest.raises(ResourceResolutionError):
        resolve_resource_ids("nonexistent", _INVENTORY)


def test_empty_name_raises():
    with pytest.raises(ResourceResolutionError):
        resolve_resource_ids("   ", _INVENTORY)


def test_instance_match_takes_priority_over_cluster_substring():
    # An instance identifier that happens to also be a substring
    # elsewhere should still resolve to itself alone via the exact
    # instance match, not expand to a cluster.
    result = resolve_resource_ids("rds-devops", _INVENTORY)
    assert result == ["rds-devops"]
