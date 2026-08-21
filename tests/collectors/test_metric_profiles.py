"""Tests for src.collectors.metric_profiles (no AWS calls)."""

import pytest

from src.collectors.metric_profiles import (
    resolve_cluster_metric_profile,
)


def test_resolve_cluster_metric_profile_includes_volume_bytes_used_for_aurora():
    selection = resolve_cluster_metric_profile(
        cluster={"engine": "aurora-postgresql"}
    )

    assert selection.metrics == ("VolumeBytesUsed",)
    assert selection.resource_profile == "aurora-cluster"


def test_resolve_cluster_metric_profile_returns_no_metrics_for_non_aurora():
    selection = resolve_cluster_metric_profile(
        cluster={"engine": "postgres"}
    )

    assert selection.metrics == ()
    assert selection.resource_profile == "cluster-generic"


def test_resolve_cluster_metric_profile_rejects_unsupported_profile():
    with pytest.raises(ValueError):
        resolve_cluster_metric_profile(
            cluster={"engine": "aurora-mysql"},
            profile_name="not-a-real-profile",
        )
