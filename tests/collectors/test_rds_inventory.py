"""Tests for src.collectors.rds_inventory (no real AWS calls)."""

from unittest.mock import MagicMock

from src.collectors.rds_inventory import (
    collect_rds_clusters,
    collect_rds_instances,
    collect_rds_inventory,
)


def _session_with_paginator(pages: dict[str, list[dict]]):
    """Build a session whose rds client's paginators return `pages`.

    `pages` maps a paginator operation name (e.g. "describe_db_clusters")
    to the list of page dicts its paginate() should yield.
    """

    client = MagicMock()

    def get_paginator(operation_name):
        paginator = MagicMock()
        paginator.paginate.return_value = iter(
            pages.get(operation_name, [])
        )
        return paginator

    client.get_paginator.side_effect = (
        get_paginator
    )

    session = MagicMock()
    session.client.return_value = client

    return session


# --- collect_rds_clusters -------------------------------------------------


def test_collect_rds_clusters_builds_a_cluster_row():
    session = _session_with_paginator(
        {
            "describe_db_clusters": [
                {
                    "DBClusters": [
                        {
                            "DBClusterIdentifier": "watchcon-cluster",
                            "Engine": "aurora-postgresql",
                            "EngineVersion": "15.4",
                            "Status": "available",
                            "DBClusterMembers": [],
                        }
                    ]
                }
            ]
        }
    )

    clusters, _ = collect_rds_clusters(
        session=session
    )

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["resource_type"] == "db_cluster"
    assert (
        cluster["identifier"]
        == "watchcon-cluster"
    )
    assert cluster["engine"] == "aurora-postgresql"
    assert cluster["members"] == []


def test_collect_rds_clusters_marks_the_writer_and_reader():
    session = _session_with_paginator(
        {
            "describe_db_clusters": [
                {
                    "DBClusters": [
                        {
                            "DBClusterIdentifier": "watchcon-cluster",
                            "DBClusterMembers": [
                                {
                                    "DBInstanceIdentifier": "watchcon-a",
                                    "IsClusterWriter": True,
                                    "PromotionTier": 0,
                                },
                                {
                                    "DBInstanceIdentifier": "watchcon-c",
                                    "IsClusterWriter": False,
                                    "PromotionTier": 1,
                                },
                            ],
                        }
                    ]
                }
            ]
        }
    )

    clusters, instance_roles = (
        collect_rds_clusters(session=session)
    )

    members = {
        m["identifier"]: m["role"]
        for m in clusters[0]["members"]
    }
    assert members == {
        "watchcon-a": "writer",
        "watchcon-c": "reader",
    }
    assert instance_roles["watchcon-a"] == {
        "cluster_identifier": "watchcon-cluster",
        "cluster_role": "writer",
    }
    assert instance_roles["watchcon-c"] == {
        "cluster_identifier": "watchcon-cluster",
        "cluster_role": "reader",
    }


def test_collect_rds_clusters_paginates_across_pages():
    session = _session_with_paginator(
        {
            "describe_db_clusters": [
                {
                    "DBClusters": [
                        {
                            "DBClusterIdentifier": "cluster-1",
                            "DBClusterMembers": [],
                        }
                    ]
                },
                {
                    "DBClusters": [
                        {
                            "DBClusterIdentifier": "cluster-2",
                            "DBClusterMembers": [],
                        }
                    ]
                },
            ]
        }
    )

    clusters, _ = collect_rds_clusters(
        session=session
    )

    assert [
        c["identifier"] for c in clusters
    ] == ["cluster-1", "cluster-2"]


def test_collect_rds_clusters_handles_no_clusters():
    session = _session_with_paginator(
        {"describe_db_clusters": [{"DBClusters": []}]}
    )

    clusters, instance_roles = (
        collect_rds_clusters(session=session)
    )

    assert clusters == []
    assert instance_roles == {}


# --- collect_rds_instances -------------------------------------------------


def test_collect_rds_instances_builds_an_instance_row():
    session = _session_with_paginator(
        {
            "describe_db_instances": [
                {
                    "DBInstances": [
                        {
                            "DBInstanceIdentifier": "watchcon-a",
                            "DbiResourceId": "db-ABC123",
                            "Engine": "aurora-postgresql",
                            "EngineVersion": "15.4",
                            "DBInstanceClass": "db.r6g.large",
                            "AvailabilityZone": "ap-northeast-2a",
                            "DBInstanceStatus": "available",
                            "MultiAZ": False,
                            "StorageType": "aurora",
                            "PerformanceInsightsEnabled": True,
                        }
                    ]
                }
            ]
        }
    )

    instances = collect_rds_instances(
        session=session, instance_roles={}
    )

    assert len(instances) == 1
    instance = instances[0]
    assert instance["identifier"] == "watchcon-a"
    assert (
        instance["dbi_resource_id"]
        == "db-ABC123"
    )
    assert (
        instance["performance_insights_enabled"]
        is True
    )


def test_collect_rds_instances_defaults_pi_enabled_to_false_when_absent():
    session = _session_with_paginator(
        {
            "describe_db_instances": [
                {
                    "DBInstances": [
                        {
                            "DBInstanceIdentifier": "rds-devops"
                        }
                    ]
                }
            ]
        }
    )

    instances = collect_rds_instances(
        session=session, instance_roles={}
    )

    assert (
        instances[0][
            "performance_insights_enabled"
        ]
        is False
    )
    assert (
        instances[0]["dbi_resource_id"] is None
    )


def test_collect_rds_instances_prefers_its_own_cluster_identifier_over_the_role_map():
    session = _session_with_paginator(
        {
            "describe_db_instances": [
                {
                    "DBInstances": [
                        {
                            "DBInstanceIdentifier": "watchcon-a",
                            "DBClusterIdentifier": "watchcon-cluster",
                        }
                    ]
                }
            ]
        }
    )

    instances = collect_rds_instances(
        session=session,
        instance_roles={
            "watchcon-a": {
                "cluster_identifier": "stale-cluster-name",
                "cluster_role": "writer",
            }
        },
    )

    assert (
        instances[0]["cluster_identifier"]
        == "watchcon-cluster"
    )
    assert (
        instances[0]["cluster_role"] == "writer"
    )


def test_collect_rds_instances_falls_back_to_the_role_map_for_the_cluster_identifier():
    # A standard (non-Aurora) member of a Multi-AZ DB cluster reports no
    # DBClusterIdentifier of its own -- the mapping built from
    # collect_rds_clusters is the only source for it in that case.
    session = _session_with_paginator(
        {
            "describe_db_instances": [
                {
                    "DBInstances": [
                        {
                            "DBInstanceIdentifier": "watchcon-a"
                        }
                    ]
                }
            ]
        }
    )

    instances = collect_rds_instances(
        session=session,
        instance_roles={
            "watchcon-a": {
                "cluster_identifier": "watchcon-cluster",
                "cluster_role": "reader",
            }
        },
    )

    assert (
        instances[0]["cluster_identifier"]
        == "watchcon-cluster"
    )
    assert (
        instances[0]["cluster_role"] == "reader"
    )


def test_collect_rds_instances_leaves_a_standalone_instance_without_a_cluster():
    session = _session_with_paginator(
        {
            "describe_db_instances": [
                {
                    "DBInstances": [
                        {
                            "DBInstanceIdentifier": "secret-test-maria"
                        }
                    ]
                }
            ]
        }
    )

    instances = collect_rds_instances(
        session=session, instance_roles={}
    )

    assert (
        instances[0]["cluster_identifier"]
        is None
    )
    assert instances[0]["cluster_role"] is None


# --- collect_rds_inventory (integration of both) --------------------------


def test_collect_rds_inventory_wires_cluster_roles_into_instances():
    session = _session_with_paginator(
        {
            "describe_db_clusters": [
                {
                    "DBClusters": [
                        {
                            "DBClusterIdentifier": "watchcon-cluster",
                            "DBClusterMembers": [
                                {
                                    "DBInstanceIdentifier": "watchcon-a",
                                    "IsClusterWriter": True,
                                }
                            ],
                        }
                    ]
                }
            ],
            "describe_db_instances": [
                {
                    "DBInstances": [
                        {
                            "DBInstanceIdentifier": "watchcon-a"
                        }
                    ]
                }
            ],
        }
    )

    inventory = collect_rds_inventory(
        session=session
    )

    assert len(inventory["clusters"]) == 1
    assert len(inventory["instances"]) == 1
    assert (
        inventory["instances"][0][
            "cluster_identifier"
        ]
        == "watchcon-cluster"
    )
    assert (
        inventory["instances"][0][
            "cluster_role"
        ]
        == "writer"
    )
