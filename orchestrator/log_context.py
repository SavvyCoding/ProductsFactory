"""
Structured logging helpers for the orchestrator.

Contextvars carry correlation fields (product_id, session_id, persona, session_uid)
across the call stack so every log record automatically includes them — no need
to thread parameters through every function.

Two output formats are supported, selected by the LOG_FORMAT env var:

  LOG_FORMAT=text (default) — human-readable single-line format, backwards
    compatible with existing grep patterns in deploy/ scripts.
  LOG_FORMAT=json            — one JSON object per line, for ingestion into
    Loki/Datadog/CloudWatch/etc.

Usage:

    from orchestrator.log_context import log_scope
    with log_scope(product_id=42, persona="coder", session_uid="abc123"):
        log.info("starting session")  # record carries the scope fields

No dependency on python-json-logger — stdlib-only implementation so the
orchestrator keeps its tiny install footprint.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
from contextlib import contextmanager
from typing import Any


# Fields included as structured context on every record while the scope is active.
# contextvars propagate across threads when threads copy the parent context (the
# poller's background threads do — see `threading.Thread(target=..., args=...)`
# — but NOT automatically across daemons). For the threads that genuinely need
# the parent scope (log streaming, live-poll), we either inherit implicitly via
# `threading.Thread` defaults, or we re-enter the scope inside the thread body.

_log_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "pf_log_context", default={}
)


@contextmanager
def log_scope(**fields: Any):
    """Push key/value fields onto the structured log context for the duration of
    the `with` block. Nested scopes merge (inner wins on key collision)."""
    current = _log_context.get()
    merged = {**current, **{k: v for k, v in fields.items() if v is not None}}
    token = _log_context.set(merged)
    try:
        yield
    finally:
        _log_context.reset(token)


def current_context() -> dict[str, Any]:
    """Return a copy of the active context fields — used by formatters."""
    return dict(_log_context.get())


# ── Formatters ────────────────────────────────────────────────────────────────

class _ContextAwareTextFormatter(logging.Formatter):
    """Text formatter that appends context fields in brackets when present."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        ctx = current_context()
        if not ctx:
            return base
        kvs = " ".join(f"{k}={v}" for k, v in ctx.items())
        return f"{base} [{kvs}]"


class JSONFormatter(logging.Formatter):
    """Minimal JSON formatter — one object per log line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts":     self.formatTime(record, self.datefmt),
            "level":  record.levelname,
            "logger": record.name,
            "msg":    record.getMessage(),
        }
        # Merge current scope fields, but don't let them clobber the reserved keys above.
        for k, v in current_context().items():
            if k not in payload:
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def build_formatter() -> logging.Formatter:
    """Return the configured formatter based on LOG_FORMAT."""
    fmt_name = os.environ.get("LOG_FORMAT", "text").lower()
    if fmt_name == "json":
        return JSONFormatter()
    return _ContextAwareTextFormatter("%(asctime)s [%(levelname)s] %(message)s")
