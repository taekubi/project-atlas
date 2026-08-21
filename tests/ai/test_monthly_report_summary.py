"""Tests for the prompt-formatting logic in src.ai.monthly_report_summary."""

from src.ai.monthly_report_summary import (
    _format_report_entry,
    format_report_prompt,
)

_BYTES_PER_GB = 1024**3


def _entry(**overrides) -> dict:
    entry = {
        "resource_id": "watchcon-a",
        "month": "2026-08",
        "previous_month": "2026-07",
        "engine": "aurora-postgresql",
        "cluster_role": "writer",
        "active_days": 31,
        "days_in_month": 31,
        "previous_active_days": 31,
        "coverage_note": None,
        "cpu_avg": 44.0,
        "cpu_avg_previous": 40.0,
        "cpu_avg_change_pct": 10.0,
    }
    entry.update(overrides)
    return entry


def test_entry_shows_current_previous_and_change_together():
    # The percentage alone is not reportable -- a reader needs both
    # figures behind it.
    rendered = _format_report_entry(_entry())

    assert (
        "cpu_avg=44.0 (prev=40.0, change=10.0%)"
        in rendered
    )


def test_entry_shows_a_value_without_a_comparison():
    rendered = _format_report_entry(
        _entry(
            cpu_avg_previous=None,
            cpu_avg_change_pct=None,
        )
    )

    assert "cpu_avg=44.0" in rendered
    assert "prev=" not in rendered
    assert "change=" not in rendered


def test_entry_omits_metrics_with_no_data():
    rendered = _format_report_entry(
        _entry(cpu_avg=None)
    )

    assert "cpu_avg" not in rendered


def test_entry_carries_coverage_for_the_model_to_caveat():
    rendered = _format_report_entry(
        _entry(
            active_days=6,
            coverage_note="partial_month",
        )
    )

    assert "active_days=6" in rendered
    assert "days_in_month=31" in rendered
    assert (
        "coverage_note=partial_month" in rendered
    )


def test_entry_converts_storage_bytes_to_gb():
    rendered = _format_report_entry(
        _entry(
            free_storage_space_min_bytes=(
                50 * _BYTES_PER_GB
            ),
            volume_bytes_used_max=(
                120 * _BYTES_PER_GB
            ),
        )
    )

    assert (
        "free_storage_space_min_gb=50.0"
        in rendered
    )
    assert (
        "volume_bytes_used_max_gb=120.0"
        in rendered
    )


def test_prompt_states_the_month_and_scope():
    prompt = format_report_prompt(
        report=[_entry()],
        month="2026-08",
        account_id="826846563965",
        region="ap-northeast-2",
    )

    assert "report_month=2026-08" in prompt
    assert "account_id=826846563965" in prompt
    assert "region=ap-northeast-2" in prompt
    assert "resource_count=1" in prompt


def test_prompt_includes_every_resource():
    prompt = format_report_prompt(
        report=[
            _entry(resource_id="watchcon-a"),
            _entry(resource_id="watchcon-c"),
        ],
        month="2026-08",
        account_id="826846563965",
        region="ap-northeast-2",
    )

    assert "watchcon-a" in prompt
    assert "watchcon-c" in prompt
    assert "resource_count=2" in prompt
