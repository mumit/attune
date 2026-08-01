"""Bounded, documented read-tool response shapes (build prompt 34, task 3).

Six read-only tools, per the build prompt: ``attune.brief``,
``attune.what_matters``, ``attune.importance``, ``attune.memory.search``,
``attune.pending``, ``attune.playbook.show``. Each returns a **bounded,
documented response shape** -- the same data-minimization discipline
``hosted/google_provider.py`` already applies (bounded response size, field
stripping) -- built from a view dataclass here, never the internal
``Brief``/``Bullet``/``MemoryRecord`` dataclasses directly, so an internal
refactor of those doesn't silently change what a calling agent can observe.

:class:`RuntimeReadPort` is the ONLY thing :class:`~attune.mcp_server.server.AttuneMcpServer`
depends on for data access -- a plain, credential-free Protocol. The real
implementation (assembling a brief, querying memory, reading the playbook)
runs inside the credential-holding runtime process and is reached over
whatever private internal boundary that process exposes (mirroring the
stateless republisher / hosted broker pattern -- see ``docs/decisions.md``);
this module never imports ``brief.py``, ``memory``, or a connector, so the
MCP server process itself never needs to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

# Data-minimization bounds, mirroring ingestion/sources.py's TEXT_CHAR_CAP
# and brief.py's SNAPSHOT_LIST_CAP precedents: every list a tool returns is
# capped in item count, every text field capped in length, regardless of
# how much the underlying store actually holds.
MAX_TEXT_CHARS = 4000
MAX_LIST_ITEMS = 50


def _bounded_text(value: "str | None", limit: int = MAX_TEXT_CHARS) -> str:
    if not value:
        return ""
    return value[:limit]


def _bounded_list(items: "list[Any]", limit: int = MAX_LIST_ITEMS) -> list[Any]:
    return items[:limit]


@dataclass(frozen=True)
class MeetingView:
    event_id: str
    summary: str
    start: str
    end: str
    notes: "tuple[str, ...]" = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": _bounded_text(self.event_id, 256),
            "summary": _bounded_text(self.summary, 256),
            "start": self.start,
            "end": self.end,
            "notes": [_bounded_text(n, 500) for n in self.notes[:10]],
        }


@dataclass(frozen=True)
class ThreadRefView:
    thread_id: str
    subject: str
    from_addr: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": _bounded_text(self.thread_id, 256),
            "subject": _bounded_text(self.subject, 256),
            "from": _bounded_text(self.from_addr, 256),
        }


@dataclass(frozen=True)
class BriefView:
    """``attune.brief``'s bounded response: today's assembled brief."""

    generated_at: str
    unread_count: int
    event_count: int
    summary: str
    meetings: "tuple[MeetingView, ...]" = ()
    waiting_on: "tuple[ThreadRefView, ...]" = ()
    spine: "tuple[str, ...]" = ()
    since_yesterday: "tuple[str, ...]" = ()
    pending_tally: "str | None" = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "unread_count": int(self.unread_count),
            "event_count": int(self.event_count),
            "summary": _bounded_text(self.summary),
            "meetings": [m.to_dict() for m in _bounded_list(list(self.meetings), 20)],
            "waiting_on": [w.to_dict() for w in _bounded_list(list(self.waiting_on), 20)],
            "spine": [_bounded_text(s, 500) for s in _bounded_list(list(self.spine))],
            "since_yesterday": [
                _bounded_text(s, 500) for s in _bounded_list(list(self.since_yesterday))
            ],
            "pending_tally": _bounded_text(self.pending_tally, 256) or None,
        }


@dataclass(frozen=True)
class ImportanceView:
    """``attune.importance``'s bounded response: the tier and reason for a
    sender -- inspectable learning, the product's actual differentiator."""

    sender: str
    tier: str
    reason: str
    pinned: bool
    probation: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "sender": _bounded_text(self.sender, 320),
            "tier": self.tier,
            "reason": _bounded_text(self.reason, 500),
            "pinned": bool(self.pinned),
            "probation": bool(self.probation),
        }


@dataclass(frozen=True)
class MemoryHitView:
    memory_id: str
    text: str
    score: "float | None"

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": _bounded_text(self.memory_id, 128),
            "text": _bounded_text(self.text, 1000),
            "score": self.score,
        }


@dataclass(frozen=True)
class PendingItemView:
    """One entry of ``attune.pending``'s bounded response: a proposal
    awaiting a human decision."""

    proposal_ref: str
    source_ref: str
    domain: str
    subject: "str | None"
    priority: "str | None"
    action: "str | None"
    posted_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_ref": _bounded_text(self.proposal_ref, 256),
            "source_ref": _bounded_text(self.source_ref, 256),
            "domain": self.domain,
            "subject": _bounded_text(self.subject, 256) or None,
            "priority": self.priority,
            "action": self.action,
            "posted_at": self.posted_at,
        }


class RuntimeReadPort(Protocol):
    """The one boundary :class:`~attune.mcp_server.server.AttuneMcpServer`
    crosses to answer a read tool. Every method takes no principal/tenant
    argument at all -- there is exactly one principal per Attune instance
    (``docs/design.md``), so there is no selection surface for a calling
    agent's arguments to attack in the first place."""

    def brief(self) -> BriefView: ...

    def what_matters(self) -> "tuple[str, ...]": ...

    def importance(self, sender: str) -> "ImportanceView | None": ...

    def memory_search(self, query: str, *, limit: int) -> "tuple[MemoryHitView, ...]": ...

    def pending(self) -> "tuple[PendingItemView, ...]": ...

    def playbook_show(self, domain: "str | None") -> str: ...


# ---------------------------------------------------------------------------
# Read-tool argument contracts and dispatch table.
# ---------------------------------------------------------------------------

MEMORY_SEARCH_MAX_LIMIT = 20


class ReadArgumentError(Exception):
    """A read tool's arguments were malformed -- refused, never partially
    honored."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _tool_brief(reader: RuntimeReadPort, arguments: Mapping[str, Any]) -> dict[str, Any]:
    if arguments:
        raise ReadArgumentError("arguments_invalid")
    return reader.brief().to_dict()


def _tool_what_matters(reader: RuntimeReadPort, arguments: Mapping[str, Any]) -> dict[str, Any]:
    if arguments:
        raise ReadArgumentError("arguments_invalid")
    return {"spine": [_bounded_text(s, 500) for s in _bounded_list(list(reader.what_matters()))]}


def _tool_importance(reader: RuntimeReadPort, arguments: Mapping[str, Any]) -> dict[str, Any]:
    if set(arguments) != {"sender"} or not isinstance(arguments["sender"], str):
        raise ReadArgumentError("arguments_invalid")
    sender = arguments["sender"]
    if not 1 <= len(sender) <= 320:
        raise ReadArgumentError("arguments_invalid")
    view = reader.importance(sender)
    if view is None:
        return {"sender": _bounded_text(sender, 320), "tier": None, "reason": "", "pinned": False, "probation": False}
    return view.to_dict()


def _tool_memory_search(reader: RuntimeReadPort, arguments: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"query", "limit"}
    if not set(arguments) <= allowed or "query" not in arguments:
        raise ReadArgumentError("arguments_invalid")
    query = arguments["query"]
    if not isinstance(query, str) or not 1 <= len(query) <= 500:
        raise ReadArgumentError("arguments_invalid")
    limit = arguments.get("limit", 8)
    if type(limit) is not int or not 1 <= limit <= MEMORY_SEARCH_MAX_LIMIT:
        raise ReadArgumentError("arguments_invalid")
    hits = reader.memory_search(query, limit=limit)
    return {"hits": [h.to_dict() for h in _bounded_list(list(hits), MEMORY_SEARCH_MAX_LIMIT)]}


def _tool_pending(reader: RuntimeReadPort, arguments: Mapping[str, Any]) -> dict[str, Any]:
    if arguments:
        raise ReadArgumentError("arguments_invalid")
    return {"proposals": [p.to_dict() for p in _bounded_list(list(reader.pending()))]}


def _tool_playbook_show(reader: RuntimeReadPort, arguments: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"domain"}
    if not set(arguments) <= allowed:
        raise ReadArgumentError("arguments_invalid")
    domain = arguments.get("domain")
    if domain is not None and (not isinstance(domain, str) or not 1 <= len(domain) <= 80):
        raise ReadArgumentError("arguments_invalid")
    return {"domain": domain, "playbook": _bounded_text(reader.playbook_show(domain), 8000)}


READ_TOOLS: "dict[str, Any]" = {
    "attune.brief": _tool_brief,
    "attune.what_matters": _tool_what_matters,
    "attune.importance": _tool_importance,
    "attune.memory.search": _tool_memory_search,
    "attune.pending": _tool_pending,
    "attune.playbook.show": _tool_playbook_show,
}
