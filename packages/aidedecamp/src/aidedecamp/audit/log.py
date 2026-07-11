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
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Protocol


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
        known = {"thread_id", "workflow", "event", "ts", "domain", "user_id"}
        return cls(
            thread_id=raw.get("thread_id", ""),
            workflow=raw.get("workflow", ""),
            event=raw.get("event", ""),
            ts=raw.get("ts", ""),
            domain=raw.get("domain"),
            user_id=raw.get("user_id"),
            fields={k: v for k, v in raw.items() if k not in known},
        )


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
        file, never the original checkpoint."""
        with open(self._path, "a") as fh:
            for raw in events:
                entry = AuditEntry(
                    thread_id=thread_id,
                    workflow=workflow,
                    event=raw.get("event", ""),
                    ts=raw.get("ts", _now_iso()),
                    domain=domain,
                    user_id=user_id,
                    fields={k: v for k, v in raw.items() if k not in ("event", "ts")},
                )
                fh.write(json.dumps(entry.to_json()) + "\n")

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))
