"""Tests for src.observability.logger (no AWS calls)."""

import json
import logging

from src.observability.logger import (
    JsonFormatter,
    elapsed_ms,
    get_logger,
    request_id,
)


def _format(record: logging.LogRecord) -> dict:
    return json.loads(
        JsonFormatter().format(record)
    )


def _record(
    event: str = "query_job_started",
    level: int = logging.INFO,
    exc_info=None,
    **fields,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="atlas.test",
        level=level,
        pathname="handler.py",
        lineno=1,
        msg=event,
        args=(),
        exc_info=exc_info,
    )

    for key, value in fields.items():
        setattr(record, key, value)

    return record


def test_format_emits_the_standard_envelope():
    payload = _format(_record())

    assert payload["level"] == "INFO"
    assert payload["logger"] == "atlas.test"
    assert payload["event"] == "query_job_started"
    assert payload["timestamp"].endswith("Z")


def test_format_promotes_extra_fields_to_top_level():
    # Fields are queryable in Logs Insights only if they are real JSON
    # keys rather than interpolated into the message.
    payload = _format(
        _record(
            mode="topsql",
            target_name="watchcon-a",
            duration_ms=1420,
        )
    )

    assert payload["mode"] == "topsql"
    assert payload["target_name"] == "watchcon-a"
    assert payload["duration_ms"] == 1420


def test_format_omits_internal_logrecord_attributes():
    payload = _format(_record())

    for noise in (
        "msg",
        "args",
        "pathname",
        "lineno",
        "levelno",
    ):
        assert noise not in payload


def test_format_splits_exception_into_queryable_fields():
    try:
        raise ValueError("bad lookback")
    except ValueError:
        import sys

        payload = _format(
            _record(
                event="query_job_failed",
                level=logging.ERROR,
                exc_info=sys.exc_info(),
            )
        )

    assert payload["error_type"] == "ValueError"
    assert (
        payload["error_message"]
        == "bad lookback"
    )
    assert (
        "ValueError: bad lookback"
        in payload["traceback"]
    )


def test_format_keeps_korean_readable():
    payload = _format(
        _record(
            error_message="리소스를 찾을 수 없습니다"
        )
    )

    assert (
        payload["error_message"]
        == "리소스를 찾을 수 없습니다"
    )


def test_format_survives_a_non_serializable_field():
    # Logging must never be the thing that fails a request.
    payload = _format(
        _record(session=object())
    )

    assert "session" in payload


def test_get_logger_does_not_stack_handlers():
    # A warm Lambda container calls this on every invocation; adding a
    # handler each time would multiply every line.
    name = "atlas.test.repeat"

    first = get_logger(name)
    for _ in range(5):
        get_logger(name)

    assert len(first.handlers) == 1
    assert first.propagate is False


def test_request_id_reads_the_lambda_context():
    class Context:
        aws_request_id = "abc-123"

    assert request_id(Context()) == "abc-123"


def test_request_id_tolerates_a_missing_context():
    class Bare:
        pass

    assert request_id(None) is None
    assert request_id(Bare()) is None


def test_elapsed_ms_is_a_non_negative_integer():
    import time

    value = elapsed_ms(time.perf_counter())

    assert isinstance(value, int)
    assert value >= 0
