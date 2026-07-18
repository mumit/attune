"""A structured, append-only, retrievable reason-for-action log (design 4.7).

Every workflow already produces structured audit events as it runs — see
``orchestrator/draft_approve.py``'s ``_audit()`` helper and the ``audit_events``
accumulator field in ``DraftApproveState``. Those events live only inside the
LangGraph checkpoint, keyed by thread_id, which makes them hard to query across
workflows ("show me every autonomous send this week"). This module is the
durable, queryable home for them.

Kept deliberately simple: one JSONL file, append-on-write, linear scan on read.
That's the right amount of infrastructure for "day one," per the design
rationale (cheap early, expensive to retrofit) — a SQL/index-backed store is a
drop-in swap later behind the same two-method interface, exactly like the
MemoryStore substrate-agnostic pattern.

Security finding F1: a plain JSONL file has no tamper evidence, yet
``orchestrator/grants.py``'s ``track_records``/``suggest_graduations`` read it
back to decide autonomy graduations — an edited or deleted line would silently
skew those decisions. Every line ``record()`` appends now carries ``prev_hash``
and ``entry_hash``, chaining each entry to the one before it exactly like the
hosted service's hash-chained audit (see ``docs/audit-writer.md``), just
without that service's transactional outbox. :meth:`JsonlAuditLog.verify`
walks the chain to confirm nothing between the first and last line was edited,
deleted, reordered, or inserted, and that no unhashed line was appended after
hashing began.

That check has one honest limitation: a pure tail truncation of an
append-only file — deleting the last N lines and nothing else — is
undetectable from the file alone, because the chain only proves relationships
*among the lines present*. Detecting truncation needs an external,
independently-anchored head (the hosted service's transactional outbox plays
that role); this lightweight local file does not have one. Lines written
before hashing was introduced ("legacy" lines) are tolerated only as a prefix,
before the first hashed entry — one appearing after hashing has started is
treated as tampering, not legacy.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Protocol

from ..fslock import locked

GENESIS_HASH = "0" * 64


@dataclass
class AuditEntry:
    """One structured reason-for-action record, retrievable later.

    ``thread_id`` is the LangGraph checkpoint thread_id (e.g.
    ``"gmail:<tid>:<historyId>"``), the join key back to the workflow that
    produced this entry. ``event``/``fields`` are whatever the workflow's
    ``_audit()`` call recorded (e.g. ``event="autonomy_gate"``,
    ``fields={"action": "draft_reply", "routed_to": "approve"}``).
    """

    thread_id: str
    workflow: str
    event: str
    ts: str
    domain: str | None = None
    user_id: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "workflow": self.workflow,
            "event": self.event,
            "ts": self.ts,
            "domain": self.domain,
            "user_id": self.user_id,
            **self.fields,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "AuditEntry":
        # prev_hash/entry_hash (Fix F1's hash chain) are bookkeeping the
        # chain adds on top of the entry's own content — excluded here so
        # they never leak into `fields` and change query() results.
        known = {
            "thread_id", "workflow", "event", "ts", "domain", "user_id",
            "prev_hash", "entry_hash",
        }
        return cls(
            thread_id=raw.get("thread_id", ""),
            workflow=raw.get("workflow", ""),
            event=raw.get("event", ""),
            ts=raw.get("ts", ""),
            domain=raw.get("domain"),
            user_id=raw.get("user_id"),
            fields={k: v for k, v in raw.items() if k not in known},
        )


@dataclass(frozen=True)
class ChainVerification:
    """Result of :meth:`JsonlAuditLog.verify` walking the hash chain.

    ``checked`` counts hashed entries whose ``entry_hash``/``prev_hash`` were
    confirmed; ``legacy`` counts unhashed lines tolerated as a pre-hashing
    prefix. ``first_bad_line`` (1-based, matching a text editor's line
    numbers) and ``reason`` are set together on the first failure found —
    verification stops there rather than cataloguing every subsequent line.
    """

    ok: bool
    checked: int
    legacy: int
    first_bad_line: int | None = None
    reason: str | None = None


def _canonical_json(payload: dict[str, Any]) -> str:
    """The exact serialization that gets hashed — stable key order and no
    incidental whitespace, so the same dict always hashes the same way
    regardless of how it was constructed or round-tripped through JSON."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _entry_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    """SHA-256 of ``prev_hash`` concatenated with ``payload``'s canonical
    JSON — the link that makes editing, deleting, or reordering any earlier
    entry recompute differently from what a later entry recorded."""
    digest = hashlib.sha256()
    digest.update(prev_hash.encode("utf-8"))
    digest.update(_canonical_json(payload).encode("utf-8"))
    return digest.hexdigest()


class AuditLog(Protocol):
    """The swappable audit substrate interface."""

    def record(
        self,
        *,
        thread_id: str,
        workflow: str,
        events: list[dict[str, Any]],
        domain: str | None = None,
        user_id: str | None = None,
    ) -> None: ...

    def query(
        self,
        *,
        thread_id: str | None = None,
        domain: str | None = None,
        user_id: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[AuditEntry]: ...


class JsonlAuditLog:
    """Appends one JSON object per line to ``path``; reads back via linear scan.

    ``path``'s parent directory is created if missing so a fresh deployment
    doesn't need a manual `mkdir` step before the first write.
    """

    def __init__(self, path: str):
        self._path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def record(
        self,
        *,
        thread_id: str,
        workflow: str,
        events: list[dict[str, Any]],
        domain: str | None = None,
        user_id: str | None = None,
    ) -> None:
        """Append each raw event dict (from a graph's ``audit_events``) as one
        enriched, retrievable line: thread_id + workflow + domain/user_id
        context are stamped onto every event so a later query needs only this
        file, never the original checkpoint.

        Each line also gets ``prev_hash``/``entry_hash`` (finding F1),
        chaining it to whatever the file's last hashed line recorded. The
        read-last-hash-then-append sequence runs under
        ``fslock.locked(path + ".lock")`` — finding F2's cross-process
        lock — so two overlapping processes appending at once can't both
        read the same ``prev_hash`` and fork the chain.
        """
        with locked(self._path + ".lock"):
            prev_hash = self._last_hash()
            with open(self._path, "a") as fh:
                for raw in events:
                    entry = AuditEntry(
                        thread_id=thread_id,
                        workflow=workflow,
                        event=raw.get("event", ""),
                        ts=raw.get("ts", _now_iso()),
                        domain=domain,
                        user_id=user_id,
                        fields={
                            k: v for k, v in raw.items() if k not in ("event", "ts")
                        },
                    )
                    payload = entry.to_json()
                    entry_hash = _entry_hash(prev_hash, payload)
                    line = {**payload, "prev_hash": prev_hash, "entry_hash": entry_hash}
                    fh.write(json.dumps(line) + "\n")
                    prev_hash = entry_hash

    def _last_hash(self) -> str:
        """The hash to chain the next appended entry from: the last
        non-empty line's ``entry_hash`` if the file already has one, or
        :data:`GENESIS_HASH` if the file is missing/empty, unparseable, or
        its tail line predates hashing (a legacy, unhashed line) — matching
        :meth:`verify`'s rule that a new chain starts fresh after any
        unhashed line rather than trying to chain onto it."""
        if not os.path.exists(self._path):
            return GENESIS_HASH
        last_line = None
        with open(self._path) as fh:
            for raw_line in fh:
                stripped = raw_line.strip()
                if stripped:
                    last_line = stripped
        if last_line is None:
            return GENESIS_HASH
        try:
            raw = json.loads(last_line)
        except json.JSONDecodeError:
            return GENESIS_HASH
        entry_hash = raw.get("entry_hash")
        return entry_hash if isinstance(entry_hash, str) else GENESIS_HASH

    def query(
        self,
        *,
        thread_id: str | None = None,
        domain: str | None = None,
        user_id: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[AuditEntry]:
        """Linear scan with in-memory filtering. Fine at JSONL-file scale;
        swap the implementation, not the call sites, if that stops being true."""
        results: list[AuditEntry] = []
        for entry in self._read_all():
            if thread_id is not None and entry.thread_id != thread_id:
                continue
            if domain is not None and entry.domain != domain:
                continue
            if user_id is not None and entry.user_id != user_id:
                continue
            if since is not None and _parse_ts(entry.ts) < since:
                continue
            results.append(entry)
        if limit is not None:
            results = results[-limit:]
        return results

    def _read_all(self) -> Iterator[AuditEntry]:
        if not os.path.exists(self._path):
            return
        with open(self._path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield AuditEntry.from_json(json.loads(line))

    def verify(self) -> ChainVerification:
        """Walk the file top to bottom, recomputing each hashed entry's
        ``entry_hash`` and confirming its ``prev_hash`` matches the previous
        hashed entry's — the check Doctor runs to surface finding F1
        tampering before it silently skews ``grants.py``'s autonomy-
        graduation math. Detects:

        - an entry whose recomputed hash doesn't match its stored
          ``entry_hash`` (content was edited);
        - an entry whose ``prev_hash`` doesn't match the previous hashed
          entry's ``entry_hash`` (a line was deleted, reordered, or
          inserted);
        - an unhashed line appearing *after* hashed entries have begun
          (tampering appended to an otherwise-hashed file);
        - a line that isn't parseable JSON at all.

        Unhashed lines *before* the first hashed one are tolerated as
        ``legacy`` — this file may predate F1. A missing file verifies ok
        with ``checked=0``. See the module docstring for the one thing this
        cannot catch: pure tail truncation.
        """
        if not os.path.exists(self._path):
            return ChainVerification(ok=True, checked=0, legacy=0)

        checked = 0
        legacy = 0
        hashing_started = False
        running_prev = GENESIS_HASH

        with open(self._path) as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    return ChainVerification(
                        ok=False, checked=checked, legacy=legacy,
                        first_bad_line=line_no, reason="invalid JSON",
                    )

                entry_hash = raw.get("entry_hash")
                if not isinstance(entry_hash, str):
                    if hashing_started:
                        return ChainVerification(
                            ok=False, checked=checked, legacy=legacy,
                            first_bad_line=line_no,
                            reason="unhashed line appended after the chain began",
                        )
                    legacy += 1
                    continue

                hashing_started = True
                prev_hash = raw.get("prev_hash")
                if prev_hash != running_prev:
                    return ChainVerification(
                        ok=False, checked=checked, legacy=legacy,
                        first_bad_line=line_no,
                        reason=(
                            "prev_hash does not match the preceding entry — "
                            "a line was deleted, reordered, or inserted"
                        ),
                    )
                payload = {
                    k: v for k, v in raw.items() if k not in ("prev_hash", "entry_hash")
                }
                expected = _entry_hash(prev_hash, payload)
                if expected != entry_hash:
                    return ChainVerification(
                        ok=False, checked=checked, legacy=legacy,
                        first_bad_line=line_no,
                        reason="entry_hash does not match its recomputed content — line was edited",
                    )
                checked += 1
                running_prev = entry_hash

        return ChainVerification(ok=True, checked=checked, legacy=legacy)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))
