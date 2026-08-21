"""Tests for the Top SQL Slack rendering in
src.handlers.slack_command_handler (no AWS calls)."""

from src.handlers.slack_command_handler import (
    _SLACK_SQL_DISPLAY_LIMIT,
    format_top_sql_message,
)


def _sql_row(
    sql_id="abc123",
    sql_text="select * from alert",
    aas=0.2444,
    truncated=False,
):
    return {
        "sql_id": sql_id,
        "sql_text": sql_text,
        "sql_text_truncated": truncated,
        "avg_active_sessions": aas,
    }


def _analysis(queries=None, overall="전반적으로 부하가 낮습니다."):
    return {
        "overall": overall,
        "queries": queries or [],
    }


def _render(**overrides):
    kwargs = {
        "target_name": "watchcon-a",
        "label": "최근 10분",
        "top_sql_by_resource": {
            "watchcon-a": [_sql_row()]
        },
        "analysis": _analysis(),
        "resource_ids_without_pi": [],
    }
    kwargs.update(overrides)
    return format_top_sql_message(**kwargs)


# --- the ranked list is actually shown ---------------------------------


def test_message_shows_the_statement_text():
    # The whole point of this mode: the answer is the query itself, not
    # a paragraph referring to it.
    text = _render()

    assert "select * from alert" in text
    assert "```" in text


def test_message_shows_the_rank_load_and_sql_id():
    text = _render()

    assert "*1.*" in text
    assert "0.2444" in text
    assert "abc123" in text


def test_message_ranks_multiple_statements_in_order():
    text = _render(
        top_sql_by_resource={
            "watchcon-a": [
                _sql_row(
                    sql_id="first",
                    sql_text="select 1",
                    aas=0.9,
                ),
                _sql_row(
                    sql_id="second",
                    sql_text="select 2",
                    aas=0.1,
                ),
            ]
        }
    )

    assert text.index("*1.*") < text.index(
        "*2.*"
    )
    assert text.index("first") < text.index(
        "second"
    )


def test_message_includes_the_header_and_overall_assessment():
    text = _render()

    assert (
        "*watchcon-a Top SQL 분석* (최근 10분)"
        in text
    )
    assert (
        "전반적으로 부하가 낮습니다." in text
    )


# --- per-query findings -------------------------------------------------


def test_message_attaches_a_finding_to_its_own_statement():
    text = _render(
        analysis=_analysis(
            queries=[
                {
                    "resource_id": "watchcon-a",
                    "rank": 1,
                    "finding": "WHERE 절이 없어 전체 스캔이 발생합니다.",
                    "suggestion": "조건절 추가를 검토하세요.",
                    "confidence": "high",
                }
            ]
        )
    )

    assert (
        "WHERE 절이 없어 전체 스캔이 발생합니다."
        in text
    )
    assert (
        "제안: 조건절 추가를 검토하세요." in text
    )
    assert "가능성 높음" in text


def test_message_omits_the_suggestion_line_when_there_is_none():
    text = _render(
        analysis=_analysis(
            queries=[
                {
                    "resource_id": "watchcon-a",
                    "rank": 1,
                    "finding": "특이사항 없습니다.",
                    "suggestion": "",
                    "confidence": "medium",
                }
            ]
        )
    )

    assert "특이사항 없습니다." in text
    assert "제안:" not in text


def test_message_renders_a_statement_with_no_finding():
    # The model is told to skip statements it has nothing useful to say
    # about; those must still appear in the ranking.
    text = _render(analysis=_analysis(queries=[]))

    assert "select * from alert" in text
    assert "*1.*" in text


def test_message_does_not_attach_a_finding_to_the_wrong_rank():
    text = _render(
        top_sql_by_resource={
            "watchcon-a": [
                _sql_row(
                    sql_id="first",
                    sql_text="select 1",
                ),
                _sql_row(
                    sql_id="second",
                    sql_text="select 2",
                ),
            ]
        },
        analysis=_analysis(
            queries=[
                {
                    "resource_id": "watchcon-a",
                    "rank": 2,
                    "finding": "두번째 쿼리 지적",
                    "suggestion": "",
                    "confidence": "low",
                }
            ]
        ),
    )

    second_block_start = text.index("*2.*")
    assert (
        text.index("두번째 쿼리 지적")
        > second_block_start
    )


# --- truncation ------------------------------------------------------


def test_message_warns_when_the_statement_was_already_truncated():
    text = _render(
        top_sql_by_resource={
            "watchcon-a": [
                _sql_row(
                    sql_text="SELECT x…",
                    truncated=True,
                )
            ]
        }
    )

    assert "쿼리가 길어 일부만 표시했습니다" in text
    assert "Performance Insights" in text


def test_message_shortens_a_long_statement_for_display_and_says_so():
    long_sql = "SELECT " + ("x" * 2000)

    text = _render(
        top_sql_by_resource={
            "watchcon-a": [
                _sql_row(sql_text=long_sql)
            ]
        }
    )

    assert long_sql not in text
    assert "쿼리가 길어 일부만 표시했습니다" in text
    assert (
        long_sql[:_SLACK_SQL_DISPLAY_LIMIT]
        in text
    )


def test_message_does_not_warn_for_a_short_statement():
    text = _render()

    assert (
        "쿼리가 길어 일부만 표시했습니다"
        not in text
    )


def test_message_handles_a_missing_statement_text():
    text = _render(
        top_sql_by_resource={
            "watchcon-a": [
                _sql_row(sql_text=None)
            ]
        }
    )

    assert "(SQL 텍스트 없음)" in text


# --- multiple resources and skipped ones ---------------------------------


def test_message_labels_each_resource_when_there_is_more_than_one():
    text = _render(
        top_sql_by_resource={
            "watchcon-a": [_sql_row()],
            "watchcon-c": [_sql_row()],
        }
    )

    assert "*— watchcon-a —*" in text
    assert "*— watchcon-c —*" in text


def test_message_omits_the_resource_label_for_a_single_resource():
    text = _render()

    assert "*— watchcon-a —*" not in text


def test_message_skips_a_resource_with_no_statements():
    text = _render(
        top_sql_by_resource={
            "watchcon-a": [_sql_row()],
            "watchcon-c": [],
        }
    )

    assert "*— watchcon-c —*" not in text


def test_message_reports_resources_excluded_for_having_pi_off():
    text = _render(
        resource_ids_without_pi=[
            "rds-devops",
            "secret-test-maria",
        ]
    )

    assert (
        "Performance Insights가 꺼져 있어" in text
    )
    assert "rds-devops" in text
    assert "secret-test-maria" in text


def test_message_always_states_that_suggestions_are_estimates():
    # Suggestions are read off the statement text without an execution
    # plan; the message must never present them as diagnoses.
    text = _render()

    assert "실행계획 확인이 필요합니다" in text
