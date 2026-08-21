"""Tests for the non-AWS-calling logic in src.handlers.slack_command_handler."""

import hashlib
import hmac
import time
from unittest.mock import MagicMock, patch

import pytest

from src.handlers.slack_command_handler import (
    SlackCommandError,
    SlackSignatureError,
    _extend_with_cluster_identifiers,
    _find_config_target,
    format_slack_message,
    parse_command_text,
    resolve_query_scope,
    verify_slack_signature,
)
from src.config.atlas_config import (
    AtlasConfig,
    AtlasSettings,
    CollectionSettings,
    TargetSettings,
)

_SECRET = "test-signing-secret"


def _sign(timestamp: str, body: str) -> str:
    basestring = f"v0:{timestamp}:{body}"
    return "v0=" + hmac.new(
        _SECRET.encode(),
        basestring.encode(),
        hashlib.sha256,
    ).hexdigest()


def test_verify_slack_signature_accepts_valid_signature():
    timestamp = str(int(time.time()))
    body = "command=%2Fatlas&text=watchcon-a"
    verify_slack_signature(
        signing_secret=_SECRET,
        timestamp=timestamp,
        body=body,
        provided_signature=_sign(timestamp, body),
    )


def test_verify_slack_signature_rejects_wrong_signature():
    timestamp = str(int(time.time()))
    body = "command=%2Fatlas&text=watchcon-a"
    with pytest.raises(SlackSignatureError):
        verify_slack_signature(
            signing_secret=_SECRET,
            timestamp=timestamp,
            body=body,
            provided_signature="v0=deadbeef",
        )


def test_verify_slack_signature_rejects_stale_timestamp():
    timestamp = str(int(time.time()) - 1000)
    body = "command=%2Fatlas&text=watchcon-a"
    with pytest.raises(SlackSignatureError):
        verify_slack_signature(
            signing_secret=_SECRET,
            timestamp=timestamp,
            body=body,
            provided_signature=_sign(timestamp, body),
        )


def test_parse_command_text_defaults_to_live_30_minutes():
    assert parse_command_text("health watchcon-a") == (
        "watchcon-a",
        "live",
        "30",
    )


def test_parse_command_text_parses_duration_minutes():
    assert parse_command_text("health watchcon-a 45m") == (
        "watchcon-a",
        "live",
        "45",
    )


def test_parse_command_text_converts_hours_to_minutes():
    assert parse_command_text("health watchcon-a 2h") == (
        "watchcon-a",
        "live",
        "120",
    )


def test_parse_command_text_parses_date():
    assert parse_command_text(
        "health watchcon-a 2026-08-19"
    ) == ("watchcon-a", "date", "2026-08-19")


def test_parse_command_text_rejects_bad_argument():
    with pytest.raises(SlackCommandError):
        parse_command_text("health watchcon-a bogus")


def test_parse_command_text_rejects_missing_health_prefix():
    with pytest.raises(SlackCommandError):
        parse_command_text("watchcon-a 최근 30분")


def test_parse_command_text_defaults_storage_to_30_days():
    assert parse_command_text("storage watchcon-a") == (
        "watchcon-a",
        "storage",
        "30",
    )


def test_parse_command_text_parses_storage_days():
    assert parse_command_text(
        "storage watchcon-a 60d"
    ) == ("watchcon-a", "storage", "60")


def test_parse_command_text_rejects_bad_storage_argument():
    with pytest.raises(SlackCommandError):
        parse_command_text("storage watchcon-a 60m")


def test_parse_command_text_defaults_topsql_to_10_minutes():
    assert parse_command_text("topsql watchcon-a") == (
        "watchcon-a",
        "topsql",
        "10",
    )


def test_parse_command_text_parses_topsql_duration_minutes():
    assert parse_command_text(
        "topsql watchcon-a 5m"
    ) == ("watchcon-a", "topsql", "5")


def test_parse_command_text_converts_topsql_hours_to_minutes():
    assert parse_command_text(
        "topsql watchcon-a 1h"
    ) == ("watchcon-a", "topsql", "60")


def test_parse_command_text_rejects_bad_topsql_argument():
    with pytest.raises(SlackCommandError):
        parse_command_text("topsql watchcon-a 2026-08-19")


def test_format_slack_message_reports_no_rows():
    text = format_slack_message(
        target_name="watchcon-a",
        label="2026-08-19",
        rows=[],
        summary="unused",
    )
    assert "조회 결과가 없습니다" in text


def test_format_slack_message_includes_target_label_and_summary():
    text = format_slack_message(
        target_name="watchcon-a",
        label="최근 30분",
        rows=[{"resource_id": "watchcon-a"}],
        summary="정상 상태입니다.",
    )
    assert "watchcon-a" in text
    assert "최근 30분" in text
    assert "정상 상태입니다." in text
    assert "DB Health" in text


def test_format_slack_message_uses_custom_title():
    text = format_slack_message(
        target_name="watchcon-a",
        label="최근 30일 추세",
        rows=[{"resource_id": "watchcon-a"}],
        summary="여유 있습니다.",
        title="스토리지 용량 예측",
    )
    assert "스토리지 용량 예측" in text


def test_extend_with_cluster_identifiers_adds_aurora_cluster():
    inventory = {
        "instances": [
            {
                "identifier": "watchcon-a",
                "cluster_identifier": (
                    "watchcon-cluster-cluster"
                ),
            },
            {
                "identifier": "watchcon-c",
                "cluster_identifier": (
                    "watchcon-cluster-cluster"
                ),
            },
        ],
    }

    extended = _extend_with_cluster_identifiers(
        ["watchcon-a", "watchcon-c"],
        inventory,
    )

    assert extended == [
        "watchcon-a",
        "watchcon-c",
        "watchcon-cluster-cluster",
    ]


def test_extend_with_cluster_identifiers_leaves_standalone_instance_unchanged():
    inventory = {
        "instances": [
            {
                "identifier": "rds-devops",
                "cluster_identifier": None,
            },
        ],
    }

    extended = _extend_with_cluster_identifiers(
        ["rds-devops"],
        inventory,
    )

    assert extended == ["rds-devops"]


def _make_config(targets):
    return AtlasConfig(
        atlas=AtlasSettings(
            storage_region="ap-northeast-2",
            bucket="b",
            s3_prefix="raw/cloudwatch",
            source_root="tmp",
        ),
        collection=CollectionSettings(
            lookback_minutes=15,
            period_seconds=300,
            metric_profile="operational-v1",
            metrics=[],
        ),
        targets=targets,
    )


def test_find_config_target_matches_by_name():
    target = TargetSettings(
        name="headquarters",
        account_id="826846563965",
        role_name="r",
        regions=["ap-northeast-2"],
        enabled=True,
    )
    config = _make_config([target])

    assert _find_config_target(config, "headquarters") is target
    assert _find_config_target(config, "watchcon-a") is None


def test_resolve_query_scope_matches_config_target_in_date_mode():
    target = TargetSettings(
        name="headquarters",
        account_id="826846563965",
        role_name="r",
        regions=["ap-northeast-2"],
        enabled=True,
    )
    config = _make_config([target])

    resolved_target, resource_ids = resolve_query_scope(
        config, "headquarters", "date"
    )

    assert resolved_target is target
    assert resource_ids is None


def test_resolve_query_scope_discovers_instances_for_config_target_in_topsql_mode():
    target = TargetSettings(
        name="headquarters",
        account_id="826846563965",
        role_name="r",
        regions=["ap-northeast-2"],
        enabled=True,
    )
    config = _make_config([target])

    inventory = {
        "clusters": [],
        "instances": [
            {"identifier": "watchcon-a"},
            {"identifier": "watchcon-c"},
        ],
    }

    with patch(
        "src.handlers.slack_command_handler."
        "_build_target_session",
        return_value=MagicMock(),
    ), patch(
        "src.handlers.slack_command_handler."
        "collect_rds_inventory",
        return_value=inventory,
    ):
        resolved_target, resource_ids = resolve_query_scope(
            config, "headquarters", "topsql"
        )

    assert resolved_target is target
    assert sorted(resource_ids) == [
        "watchcon-a",
        "watchcon-c",
    ]


def test_resolve_query_scope_raises_when_no_targets_enabled():
    config = _make_config([])

    with pytest.raises(SlackCommandError):
        resolve_query_scope(config, "watchcon-a", "date")


def test_resolve_query_scope_discovers_resources_for_unmatched_name():
    target = TargetSettings(
        name="headquarters",
        account_id="826846563965",
        role_name="r",
        regions=["ap-northeast-2"],
        enabled=True,
    )
    config = _make_config([target])

    inventory = {
        "clusters": [
            {
                "identifier": "watchcon-cluster-cluster",
                "members": [
                    {"identifier": "watchcon-a"},
                    {"identifier": "watchcon-c"},
                ],
            }
        ],
        "instances": [
            {"identifier": "watchcon-a"},
            {"identifier": "watchcon-c"},
        ],
    }

    with patch(
        "src.handlers.slack_command_handler."
        "_build_target_session",
        return_value=MagicMock(),
    ), patch(
        "src.handlers.slack_command_handler."
        "collect_rds_inventory",
        return_value=inventory,
    ):
        resolved_target, resource_ids = resolve_query_scope(
            config, "WatchCon", "live"
        )

    assert resolved_target is target
    assert sorted(resource_ids) == ["watchcon-a", "watchcon-c"]


def test_parse_command_text_parses_report_month():
    assert parse_command_text(
        "report watchcon-a 2026-07"
    ) == ("watchcon-a", "report", "2026-07")


def test_parse_command_text_defaults_report_to_last_complete_month():
    # No month given: the last finished month, never the partial
    # current one (comparing a partial month against a full one would
    # make every figure look like a drop).
    from src.query.monthly_report import (
        default_report_month,
    )

    target, mode, value = parse_command_text(
        "report watchcon-a"
    )

    assert (target, mode) == (
        "watchcon-a",
        "report",
    )
    assert value == default_report_month()


def test_parse_command_text_rejects_a_full_date_for_report():
    with pytest.raises(SlackCommandError):
        parse_command_text(
            "report watchcon-a 2026-07-15"
        )


def test_parse_command_text_rejects_an_impossible_report_month():
    with pytest.raises(SlackCommandError):
        parse_command_text(
            "report watchcon-a 2026-13"
        )


def test_resolve_query_scope_extends_report_scope_to_clusters():
    # Aurora publishes VolumeBytesUsed per cluster, so a report scoped
    # only to member instances would miss the cluster's storage.
    target = TargetSettings(
        name="headquarters",
        account_id="826846563965",
        role_name="r",
        regions=["ap-northeast-2"],
        enabled=True,
    )
    config = _make_config([target])

    inventory = {
        "clusters": [
            {
                "identifier": "watchcon-cluster",
                "members": [
                    {"identifier": "watchcon-a"}
                ],
            }
        ],
        "instances": [
            {
                "identifier": "watchcon-a",
                "cluster_identifier": (
                    "watchcon-cluster"
                ),
            }
        ],
    }

    with patch(
        "src.handlers.slack_command_handler."
        "_build_target_session",
        return_value=MagicMock(),
    ), patch(
        "src.handlers.slack_command_handler."
        "collect_rds_inventory",
        return_value=inventory,
    ):
        _, resource_ids = resolve_query_scope(
            config, "watchcon-a", "report"
        )

    assert sorted(resource_ids) == [
        "watchcon-a",
        "watchcon-cluster",
    ]
