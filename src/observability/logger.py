"""Structured JSON logging for Project Atlas.

Atlas runs almost entirely as Lambda functions, so CloudWatch Logs is the
only record of what actually happened on a request. Plain prose lines are
hard to work with once several targets, query modes, and scheduled
pipelines share one log group, so every line here is emitted as a single
JSON object instead. CloudWatch Logs Insights can then filter on fields
directly -- `filter mode = "topsql" and level = "ERROR"` -- rather than
matching substrings.

Callers pass context as keyword fields rather than formatting it into the
message:

    logger.info("query_job_started", extra={"mode": mode})

The message itself is written as a short snake_case event name so that
the same operation is greppable across invocations, and the varying parts
stay in their own queryable fields.

The Lambda runtime attaches its own handler to the root logger, so Atlas
loggers deliberately do not propagate -- otherwise every line would be
written twice, once as JSON here and once as plain text by the runtime.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
from typing import Any

_LOG_LEVEL_ENV = "ATLAS_LOG_LEVEL"
_DEFAULT_LOG_LEVEL = "INFO"

# Every attribute a LogRecord carries by default. Anything present on a
# record but absent here was supplied by the caller via `extra=` and so
# belongs in the JSON payload. Deriving the set from a throwaway record
# keeps it correct across Python versions instead of hardcoding names.
_RESERVED_RECORD_KEYS = frozenset(
    vars(
        logging.LogRecord(
            name="",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="",
            args=(),
            exc_info=None,
        )
    )
) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Render a log record as one line of JSON."""

    def format(
        self,
        record: logging.LogRecord,
    ) -> str:
        """Return the record as a compact JSON object."""

        payload: dict[str, Any] = {
            "timestamp": _iso_timestamp(
                record.created
            ),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }

        for key, value in vars(record).items():
            if key not in _RESERVED_RECORD_KEYS:
                payload[key] = value

        if record.exc_info:
            payload.update(
                _exception_fields(
                    record.exc_info
                )
            )

        # ensure_ascii=False keeps Korean messages readable in the
        # CloudWatch console; default=str means an unexpected object in
        # a field degrades to its repr instead of raising -- logging
        # must never be the thing that fails a request.
        return json.dumps(
            payload,
            ensure_ascii=False,
            default=str,
        )


def _iso_timestamp(
    created: float,
) -> str:
    """Format a record's creation time as ISO 8601 UTC with milliseconds."""

    base = time.strftime(
        "%Y-%m-%dT%H:%M:%S",
        time.gmtime(created),
    )
    milliseconds = int(
        (created - int(created)) * 1000
    )

    return f"{base}.{milliseconds:03d}Z"


def _exception_fields(
    exc_info: Any,
) -> dict[str, str]:
    """Split exception info into separately queryable fields."""

    exc_type, exc_value, _ = exc_info

    return {
        "error_type": (
            exc_type.__name__
            if exc_type
            else "UnknownError"
        ),
        "error_message": str(exc_value),
        "traceback": "".join(
            traceback.format_exception(
                *exc_info
            )
        ),
    }


def _resolve_level() -> int:
    """Return the configured log level, defaulting to INFO."""

    name = (
        os.getenv(
            _LOG_LEVEL_ENV,
            _DEFAULT_LOG_LEVEL,
        )
        .strip()
        .upper()
    )

    return getattr(
        logging,
        name,
        logging.INFO,
    )


def _log_stream() -> Any:
    """Return stdout, switched to UTF-8 if it isn't already.

    Log messages contain Korean, and Atlas modules are run both in
    Lambda (where stdout is already UTF-8) and from a local Windows
    console (where it defaults to the ANSI codepage and would mangle
    them). The CLI entry points in this project already reconfigure
    stdout the same way; doing it here means log output is correct even
    for modules invoked without one.
    """

    stream = sys.stdout
    reconfigure = getattr(
        stream,
        "reconfigure",
        None,
    )

    if reconfigure is None:
        return stream

    encoding = (
        getattr(stream, "encoding", "") or ""
    ).lower().replace("-", "")

    if encoding != "utf8":
        try:
            reconfigure(
                encoding="utf-8",
                errors="replace",
            )
        except (ValueError, OSError):
            pass

    return stream


def get_logger(
    name: str,
) -> logging.Logger:
    """Return a JSON-formatted logger for a module.

    Safe to call repeatedly: a warm Lambda container reuses the same
    logger, and re-attaching a handler each time would multiply every
    line by the number of invocations the container has served.
    """

    logger = logging.getLogger(name)

    if not getattr(
        logger,
        "_atlas_configured",
        False,
    ):
        handler = logging.StreamHandler(
            _log_stream()
        )
        handler.setFormatter(JsonFormatter())

        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(_resolve_level())
        logger._atlas_configured = True

    return logger


def request_id(
    context: Any,
) -> str | None:
    """Return the Lambda request ID from a handler context, if present.

    Every log line and every user-facing error message carries this, so
    a report of "it failed at 3pm" can be traced to the exact
    invocation without guessing from timestamps.
    """

    if context is None:
        return None

    return getattr(
        context,
        "aws_request_id",
        None,
    )


def elapsed_ms(
    started: float,
) -> int:
    """Return milliseconds elapsed since a time.perf_counter() reading."""

    return int(
        (time.perf_counter() - started) * 1000
    )
