"""Logging setup with automatic secret redaction."""

from __future__ import annotations

import logging
import sys

from workflow_agent.security import redact

LOGGER_NAME = "workflow_agent"


class RedactingFilter(logging.Filter):
    """Scrub secrets from every record before it reaches a handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        return True


class _StderrHandler(logging.StreamHandler):
    """Always writes to the *current* sys.stderr (safe when streams are swapped or captured)."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:
        pass


def setup_logging(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = _StderrHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    return logger
