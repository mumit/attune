"""Logging configuration for the always-on process (roadmap prompt 06).

Stdlib ``logging`` only — no metrics server, no Prometheus endpoint (that
would be an inbound port, rule 5). Logs are the observability surface at
this scale: the entrypoint calls :func:`configure` once, and every module
logs through its own ``logging.getLogger(__name__)``.

Two output modes: a plain human-readable line (default), or one JSON object
per line (``ATTUNE_LOG_JSON=1``) for journald / Cloud Logging ingestion.

Redaction is a writing discipline, not a filter (rule 6): callers must log
identifiers — subjects, ids, loop names, exception classes — never tokens,
credential contents, or full message bodies. The pull loops' failure paths
log Pub/Sub message ids specifically so a poison message is findable
without its payload ever entering the log stream.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

_PLAIN_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts / level / logger / message (+ exc_type)."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exc_type"] = record.exc_info[0].__name__
        return json.dumps(entry)


def configure(level: str = "INFO", json_mode: bool = False) -> None:
    """Configure the root logger once, at process start.

    Replaces any existing handlers (idempotent across re-invocation in
    tests) rather than stacking duplicates.
    """
    handler = logging.StreamHandler()
    if json_mode:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_PLAIN_FORMAT))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
