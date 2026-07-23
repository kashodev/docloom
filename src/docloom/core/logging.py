"""Structured logging — one configuration, two renderings.

The whole codebase logs through :func:`get_logger`. Where those lines *go* is
decided once, here, by the environment:

* **A terminal** gets human-readable, colourised key-values.
* **Anything else** (a Cloud Run task, a pipe) gets one JSON object per line,
  with the fields Cloud Logging reads — ``severity`` and ``message`` — so
  structured logs land in Cloud Logging with **no SDK and no credentials**: the
  platform captures stdout, and JSON-on-stdout *is* the integration. The same
  holds for any log agent that parses JSON lines.

Two ideas do the work:

* **`get_logger(__name__)`** at module scope; call it, log events with fields
  (``log.warning("unit failed", unit=3, error=...)``), never format strings.
* **Context binding** via :func:`bind` / :func:`bound`: a worker binds its
  ``run_id`` and task index once, and every subsequent line — across every
  module it calls into — carries them, so one fleet's logs are correlatable and
  filterable without threading arguments everywhere.

Configuration is read from the environment so a deployment sets it without code:

* ``DOCLOOM_LOG_LEVEL``  — ``debug`` | ``info`` (default) | ``warning`` | ``error``
* ``DOCLOOM_LOG_FORMAT`` — ``console`` | ``json`` | ``auto`` (default: console at
  a TTY, JSON otherwise)
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

_configured = False

#: Level names → stdlib levels, so ``DOCLOOM_LOG_LEVEL`` is forgiving of case.
_LEVELS = {
    "debug": logging.DEBUG, "info": logging.INFO,
    "warning": logging.WARNING, "warn": logging.WARNING,
    "error": logging.ERROR, "critical": logging.CRITICAL,
}



def _resolve_level(explicit: str | None) -> int:
    name = (explicit or os.environ.get("DOCLOOM_LOG_LEVEL") or "info").lower()
    return _LEVELS.get(name, logging.INFO)


def _use_json(explicit: str | None) -> bool:
    fmt = (explicit or os.environ.get("DOCLOOM_LOG_FORMAT") or "auto").lower()
    if fmt == "json":
        return True
    if fmt == "console":
        return False
    # auto: a human at a terminal gets console; a task/pipe gets JSON.
    return not sys.stderr.isatty()


def _cloud_logging_fields(_logger: Any, _method: str, event: dict[str, Any]) -> dict[str, Any]:
    """Rename structlog's fields to the ones Cloud Logging reads.

    ``level`` → ``severity`` (uppercase), ``event`` → ``message``. Applied only in
    JSON mode, so console output keeps structlog's native, friendlier shape.
    """
    level = event.pop("level", None)
    if level is not None:
        event["severity"] = level.upper()
    if "event" in event:
        event["message"] = event.pop("event")
    return event


def configure(*, level: str | None = None, fmt: str | None = None,
              stream: Any | None = None, force: bool = False) -> None:
    """Configure structlog once for the process. Idempotent unless ``force``.

    Called from the CLI entry point; also safe to call from a library entry (a
    Cloud Run task invokes the CLI, so configuration happens there). ``stream``
    overrides the destination (default ``sys.stderr``) — tests pass a ``StringIO``
    to capture deterministically. Tests that need a clean renderer pass ``force``.
    """
    global _configured
    if _configured and not force:
        return

    log_level = _resolve_level(level)
    json_mode = _use_json(fmt)
    destination = stream if stream is not None else sys.stderr

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,     # bound run_id/unit/task
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    if json_mode:
        renderer: list[Any] = [
            structlog.processors.dict_tracebacks,     # exceptions as structured data
            _cloud_logging_fields,
            structlog.processors.JSONRenderer(),
        ]
    else:
        renderer = [
            structlog.processors.format_exc_info,     # a readable traceback
            structlog.dev.ConsoleRenderer(),
        ]

    structlog.configure(
        processors=[*shared, *renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=destination),
        # Not cached: docloom configures once at startup, so the per-call cost is
        # irrelevant, and caching would freeze a module-level logger against the
        # config in force when it was first used — which breaks reconfiguration
        # and, more practically, test capture.
        cache_logger_on_first_use=False,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """A logger for a module. Configures on first use so a library import that
    logs before the CLI runs still produces output rather than nothing."""
    if not _configured:
        configure()
    return structlog.get_logger(name)


def bind(**fields: Any) -> None:
    """Bind fields onto every subsequent log line in this execution context.

    A worker binds ``run_id`` and its task index once at startup; all downstream
    modules' log lines then carry them. Uses contextvars, so it is correct under
    asyncio and per-thread without passing a logger around.
    """
    structlog.contextvars.bind_contextvars(**fields)


def unbind(*keys: str) -> None:
    structlog.contextvars.unbind_contextvars(*keys)


@contextmanager
def bound(**fields: Any) -> Iterator[None]:
    """Bind fields for the duration of a block, then restore — e.g. ``unit`` while
    one work unit is processed, gone again once the next is claimed."""
    tokens = structlog.contextvars.bind_contextvars(**fields)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


def clear_context() -> None:
    """Drop all bound context — used between runs in a long-lived process."""
    structlog.contextvars.clear_contextvars()
