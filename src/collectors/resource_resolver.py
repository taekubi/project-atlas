"""Resolve a loosely-typed name against real RDS/Aurora resources.

A user asking about "WatchCon" or "왓치콘 클러스터" shouldn't have to know
the exact resource_id ("watchcon-a") or that it's really two instances
(writer + reader) under one cluster. This module matches a name against
live RDS/Aurora discovery (src.collectors.rds_inventory) instead of
requiring an exact resource_id.
"""

from __future__ import annotations

from typing import Any


class ResourceResolutionError(Exception):
    """Raised when a name cannot be resolved to any RDS/Aurora resource."""


def resolve_resource_ids(
    name: str,
    inventory: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """Resolve `name` to one or more resource_ids (DB instance identifiers).

    Tried in order, stopping at the first match:
    1. exact DB instance identifier (case-insensitive)
    2. exact DB cluster identifier (case-insensitive) -- returns every
       member instance, so a cluster name naturally aggregates writer +
       reader
    3. case-insensitive substring match against cluster identifiers --
       every member of every matching cluster
    4. case-insensitive substring match against instance identifiers
    """

    clusters = inventory.get("clusters", [])
    instances = inventory.get("instances", [])

    normalized = name.strip().lower()

    if not normalized:
        raise ResourceResolutionError(
            "빈 이름은 조회할 수 없습니다."
        )

    for instance in instances:
        identifier = (
            instance.get("identifier") or ""
        )

        if identifier.lower() == normalized:
            return [identifier]

    for cluster in clusters:
        identifier = (
            cluster.get("identifier") or ""
        )

        if identifier.lower() == normalized:
            member_ids = _cluster_member_ids(
                cluster
            )

            if member_ids:
                return member_ids

    matched_clusters = [
        cluster
        for cluster in clusters
        if normalized
        in (
            cluster.get("identifier") or ""
        ).lower()
    ]

    if matched_clusters:
        member_ids = sorted(
            {
                member_id
                for cluster in matched_clusters
                for member_id in (
                    _cluster_member_ids(cluster)
                )
            }
        )

        if member_ids:
            return member_ids

    matched_instances = sorted(
        {
            instance.get("identifier")
            for instance in instances
            if normalized
            in (
                instance.get("identifier") or ""
            ).lower()
        }
        - {None}
    )

    if matched_instances:
        return matched_instances

    raise ResourceResolutionError(
        f"'{name}'과(와) 일치하는 DB/클러스터를 "
        "찾을 수 없습니다."
    )


def _cluster_member_ids(
    cluster: dict[str, Any],
) -> list[str]:
    """Return a cluster's member instance identifiers."""

    return [
        member["identifier"]
        for member in cluster.get(
            "members", []
        )
        if member.get("identifier")
    ]
