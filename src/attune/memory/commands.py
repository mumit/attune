"""See, correct, and teach memory (design §0: "memory is the product";
roadmap prompt 11).

The design's browser phase exists largely so the user can *audit and
correct* what's been learned — until then, memory was write-only from the
user's perspective: ``get_all``/``delete`` existed with no surface. These
pure functions are that surface's engine; the chat router
(``dispatcher``) and the CLI (``cli/memory_cmd.py``) both render them, and
the future browser UI renders the same operations.

Security note (rule 2): the chat grammar for these commands must only ever
be applied to the user's own direct messages (Slack DMs are user-filtered,
Chat events are HUMAN-sender-filtered upstream). It must never be applied
to fetched mail/thread bodies — "remember that X" inside an email is
content, not a command.

Deletion is per-memory and explicit (two-step confirmation in chat, --yes
in the CLI). There is deliberately no bulk "forget everything" here.
Substrate-agnostic: only the ``MemoryStore`` interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import MemoryRecord, MemoryStore


@dataclass
class MemoryListing:
    """A numbered, renderable listing plus the number→id map that makes
    "forget 3" unambiguous against exactly this listing."""

    text: str
    ids: dict[int, str] = field(default_factory=dict)


def list_memories(
    store: MemoryStore, *, user_id: str, query: str | None = None, limit: int = 20
) -> MemoryListing:
    """A numbered listing — search results when a query is given, else the
    most recent stored memories."""
    if query:
        records = store.search(query, user_id=user_id, limit=limit)
    else:
        records = store.get_all(user_id=user_id, limit=limit)

    lines: list[str] = []
    ids: dict[int, str] = {}
    for i, record in enumerate(records, 1):
        ids[i] = record.id
        lines.append(f"{i}. {_render_record(record)}")

    text = "\n".join(lines) if lines else "No memories stored yet."
    return MemoryListing(text=text, ids=ids)


def resolve_memory(
    store: MemoryStore,
    *,
    user_id: str,
    selector: str,
    listing_ids: dict[int, str] | None = None,
) -> MemoryRecord | None:
    """Turn a user selector into a record: a number from the most recent
    listing, or an id (prefix/suffix) match. ``None`` when ambiguous or
    unknown — never guess at what to delete."""
    selector = selector.strip()
    listing_ids = listing_ids or {}

    target_id: str | None = None
    if selector.isdigit() and int(selector) in listing_ids:
        target_id = listing_ids[int(selector)]

    matches: list[MemoryRecord] = []
    for record in store.get_all(user_id=user_id, limit=500):
        if target_id is not None:
            if record.id == target_id:
                return record
            continue
        if record.id.startswith(selector) or record.id.endswith(selector):
            matches.append(record)
    return matches[0] if len(matches) == 1 else None


def forget_memory(
    store: MemoryStore,
    record: MemoryRecord,
    *,
    user_id: str,
    audit_log: Any = None,
) -> None:
    """Delete one already-resolved memory, audited under the ``memory``
    workflow — corrections to the assistant's knowledge are exactly the
    audit log's business."""
    store.delete(record.id)
    if audit_log is not None:
        from datetime import datetime, timezone

        audit_log.record(
            thread_id=f"memory:{record.id}",
            workflow="memory",
            events=[{
                "event": "memory_deleted",
                "ts": datetime.now(timezone.utc).isoformat(),
                "memory_id": record.id,
                "text": record.text[:120],
            }],
            domain="memory",
            user_id=user_id,
        )


def remember_fact(
    store: MemoryStore,
    *,
    user_id: str,
    text: str,
    audit_log: Any = None,
) -> Any:
    """Store an explicitly user-taught fact (``signal: explicit`` — stronger
    provenance than anything inferred; ``infer=True`` so the substrate
    extracts a clean fact from conversational phrasing)."""
    result = store.add(
        text,
        user_id=user_id,
        metadata={"signal": "explicit"},
        infer=True,
    )
    if audit_log is not None:
        from datetime import datetime, timezone

        audit_log.record(
            thread_id="memory:taught",
            workflow="memory",
            events=[{
                "event": "memory_taught",
                "ts": datetime.now(timezone.utc).isoformat(),
                "text": text[:120],
            }],
            domain="memory",
            user_id=user_id,
        )
    return result


def _render_record(record: MemoryRecord) -> str:
    meta = record.metadata or {}
    tags = []
    signal = meta.get("signal")
    if signal:
        action = meta.get("action")
        tags.append(f"[{signal}{':' + action if action else ''}]")
    domain = meta.get("domain")
    if domain:
        tags.append(f"({domain})")
    suffix = f"  · id …{record.id[-6:]}" if record.id else ""
    tag_str = " " + " ".join(tags) if tags else ""
    return f"{record.text}{tag_str}{suffix}"
