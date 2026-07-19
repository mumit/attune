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

Security finding F3 (Low, docs/current-state.md's 2026-07-18 review):
secrets in logs were a writing discipline only, and ``docs/security-
architecture.md``'s SEC-304 says plainly that isn't enough on its own —
"regex cleanup alone is not sufficient." Taken correctly, that also means
the reverse is true: a filter alone, without the discipline above, is not
sufficient either. :class:`RedactionFilter` below adds regex cleanup as a
SECOND, independent layer on top of the discipline, not instead of it: a
small set of bounded regexes that scrub the common secret SHAPES (bearer
tokens, Google ``ya29.`` access tokens, refresh-token JSON/kwarg fields,
PEM private-key blocks, Slack bot/app tokens, ``sk-`` API keys) from both
the rendered message and any ``%``-style args, replacing each with a fixed
``[REDACTED:<kind>]`` marker. This is DEFENSE IN DEPTH, not a license to
log secrets: the filter only catches shapes it recognizes, it cannot
redact a token embedded in a full response body or an unfamiliar
credential format, and the discipline above remains the primary control.
Read this module's docstring, not the filter's presence, as the actual
contract.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

_PLAIN_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _bounded(pattern: str) -> "re.Pattern[str]":
    """Compile with re.DOTALL off and no nested quantifiers — every pattern
    below matches a bounded run of non-whitespace or a length-capped blob,
    so none of them can exhibit catastrophic backtracking regardless of
    input length (exercised by this module's "100KB line" test)."""
    return re.compile(pattern)


# Each entry: (kind, compiled pattern). Order matters only in that PEM
# blocks are matched before anything that might otherwise partially
# consume them. Every pattern is anchored on a fixed literal prefix and
# bounded by a non-whitespace/non-greedy-but-capped run — no ``.*`` over
# unbounded input, no nested repetition.
_REDACTIONS: list[tuple[str, "re.Pattern[str]"]] = [
    ("bearer_token", _bounded(r"\bBearer\s+[A-Za-z0-9\-_.~+/]{8,2000}=*")),
    ("google_access_token", _bounded(r"\bya29\.[A-Za-z0-9\-_]{8,2000}")),
    (
        "refresh_token",
        _bounded(
            r'"refresh_token"\s*:\s*"[^"]{0,2000}"'
            r'|refresh_token=[^\s&"\']{1,2000}'
        ),
    ),
    (
        "private_key",
        _bounded(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,20000}?"
            r"-----END [A-Z ]*PRIVATE KEY-----"
        ),
    ),
    ("slack_token", _bounded(r"\bx(?:oxb|oxp|app)-[A-Za-z0-9\-]{8,2000}")),
    ("api_key", _bounded(r"\bsk-[A-Za-z0-9]{8,2000}")),
]


class RedactionFilter(logging.Filter):
    """Scrub common secret shapes from a record's message AND its args.

    Paired defense with the writing discipline above (SEC-304: a filter
    alone is insufficient) — installed on the root handler by
    :func:`configure` so it runs for every logger in the process without
    each call site remembering to opt in. Rewrites ``record.msg`` and, when
    present, each string element of ``record.args`` (the ``%``-style
    values a caller passed separately, e.g. ``logger.info("token=%s", tok)``)
    so a secret can't slip through the args path just because the filter
    only looked at the pre-formatted message.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: _redact(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    _redact(a) if isinstance(a, str) else a for a in record.args
                )
        return True


def _redact(text: str) -> str:
    for kind, pattern in _REDACTIONS:
        text = pattern.sub(f"[REDACTED:{kind}]", text)
    return text


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
    # Finding F3: paired defense-in-depth on top of the writing discipline —
    # see RedactionFilter's docstring and this module's docstring for what
    # it does and does not cover.
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
