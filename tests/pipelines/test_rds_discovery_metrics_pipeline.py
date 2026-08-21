"""Tests for the pure filtering logic in
src.pipelines.rds_discovery_metrics_pipeline (no AWS calls)."""

from src.pipelines.rds_discovery_metrics_pipeline import (
    select_available_clusters,
    select_available_instances,
)


# --- select_available_instances -------------------------------------


def test_select_available_instances_keeps_available_ones():
    inventory = {
        "instances": [
            {
                "resource_id": "watchcon-a",
                "status": "available",
            }
        ],
        "clusters": [],
    }

    assert select_available_instances(
        inventory
    ) == inventory["instances"]


def test_select_available_instances_excludes_a_non_available_status():
    inventory = {
        "instances": [
            {
                "resource_id": "watchcon-a",
                "status": "backing-up",
            }
        ],
        "clusters": [],
    }

    assert (
        select_available_instances(inventory)
        == []
    )


def test_select_available_instances_excludes_one_with_no_resource_id():
    inventory = {
        "instances": [
            {
                "resource_id": None,
                "status": "available",
            }
        ],
        "clusters": [],
    }

    assert (
        select_available_instances(inventory)
        == []
    )


def test_select_available_instances_filters_a_mixed_list():
    inventory = {
        "instances": [
            {
                "resource_id": "watchcon-a",
                "status": "available",
            },
            {
                "resource_id": "watchcon-c",
                "status": "stopped",
            },
            {
                "resource_id": "rds-devops",
                "status": "available",
            },
        ],
        "clusters": [],
    }

    result = select_available_instances(
        inventory
    )

    assert [
        r["resource_id"] for r in result
    ] == ["watchcon-a", "rds-devops"]


# --- select_available_clusters -----------------------------------------


def test_select_available_clusters_keeps_available_ones():
    inventory = {
        "clusters": [
            {
                "identifier": "watchcon-cluster",
                "status": "available",
            }
        ],
        "instances": [],
    }

    assert select_available_clusters(
        inventory
    ) == inventory["clusters"]


def test_select_available_clusters_excludes_a_non_available_status():
    inventory = {
        "clusters": [
            {
                "identifier": "watchcon-cluster",
                "status": "creating",
            }
        ],
        "instances": [],
    }

    assert (
        select_available_clusters(inventory)
        == []
    )


def test_select_available_clusters_excludes_one_with_no_identifier():
    inventory = {
        "clusters": [
            {
                "identifier": None,
                "status": "available",
            }
        ],
        "instances": [],
    }

    assert (
        select_available_clusters(inventory)
        == []
    )
