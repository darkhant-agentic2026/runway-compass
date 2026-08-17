"""Structured JSON logging.

docs/01-architecture.md asks for structured JSON logs carrying `uid`, `project_id`,
`run_id`, `turn_id`, and `invocation_id`. Cloud Logging picks up `severity` and
`message` from a JSON line on stdout without any agent configuration, so this is the
whole integration.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

#: Correlation fields lifted to the top level of the log line when present on a record.
CONTEXT_FIELDS = ("uid", "project_id", "task_id", "run_id", "turn_id", "invocation_id")

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__)


class JsonFormatter(logging.Formatter):
    """Render a record as a single JSON object on one line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            # Cloud Logging maps `severity`, not `level`.
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        for field in CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        # Anything else passed via `extra=` rides along rather than being dropped.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Install the root handler. Idempotent — safe to call from tests and from lifespan."""
    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)-8s %(name)s %(message)s"))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own colourised handlers; let them propagate to ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
