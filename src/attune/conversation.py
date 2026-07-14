"""Ephemeral conversation windows for Q&A (design 2.1's working memory).

``dispatcher._converse`` answered every message in isolation — a follow-up
like "what about the second one?" was a non-sequitur because the assistant
literally could not see its previous turn. This module is the fix: a small
rolling window of recent turns per ``(channel, user)``, replayed into the
model call and trimmed by turn count and age.

**This is working memory, not the MemoryStore.** Design 2.1's first row
(single-episode lifespan) versus everything ``memory/`` handles. Nothing here
calls ``store.add``; no fact extraction, no learning, no retrieval — turns
age out on a TTL precisely so stale context doesn't leak into tomorrow's
questions. If a Q&A exchange ever deserves to become a durable memory, that's
an explicit capture decision elsewhere, never a side effect of chatting.
That boundary is the kind that erodes — keep it hard.

Provenance discipline (rule 2): incoming chat text is stored *with* its
``[UNTRUSTED chat]`` frame, so replayed history carries the same framing it
had live. History is replayed as user/assistant turns only — never promoted
into system/instruction content.

``ConversationLog`` is a Protocol with a JSON-file-backed implementation,
same shape as ``ingestion/state.py``: read fully, rewrite fully, fine at
single-principal scale.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

DEFAULT_MAX_TURNS = 10
DEFAULT_TTL_MINUTES = 120


class ConversationLog(Protocol):
    def recent(
        self, *, channel: str, user_id: str, now: datetime | None = None
    ) -> list[dict[str, str]]:
        """Unexpired turns for one (channel, user), oldest first, each
        ``{"role": "user"|"assistant", "content": ...}``."""
        ...

    def append(
        self,
        *,
        channel: str,
        user_id: str,
        role: str,
        content: str,
        now: datetime | None = None,
    ) -> None:
        """Record one turn, evicting anything beyond the window/TTL."""
        ...


class JsonConversationLog:
    """File-backed window: ``{"<channel>:<user_id>": [{role, content, ts}]}``.

    Eviction is enforced on both paths: ``append`` trims to ``max_turns`` and
    ``recent`` drops turns older than ``ttl`` — so a window that sat on disk
    overnight comes back empty even though nothing rewrote the file.
    """

    def __init__(
        self,
        path: str,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        ttl_minutes: int = DEFAULT_TTL_MINUTES,
    ):
        self._path = path
        self._max_turns = max_turns
        self._ttl = timedelta(minutes=ttl_minutes)

    def recent(
        self, *, channel: str, user_id: str, now: datetime | None = None
    ) -> list[dict[str, str]]:
        now = now or datetime.now(timezone.utc)
        turns = self._load().get(self._key(channel, user_id), [])
        return [
            {"role": t["role"], "content": t["content"]}
            for t in turns
            if now - datetime.fromisoformat(t["ts"]) < self._ttl
        ]

    def append(
        self,
        *,
        channel: str,
        user_id: str,
        role: str,
        content: str,
        now: datetime | None = None,
    ) -> None:
        now = now or datetime.now(timezone.utc)
        data = self._load()
        key = self._key(channel, user_id)
        turns = data.get(key, [])
        turns.append({"role": role, "content": content, "ts": now.isoformat()})
        # Trim expired turns while we're writing anyway, then cap the window.
        turns = [
            t
            for t in turns
            if now - datetime.fromisoformat(t["ts"]) < self._ttl
        ][-self._max_turns:]
        data[key] = turns
        self._save(data)

    @staticmethod
    def _key(channel: str, user_id: str) -> str:
        return f"{channel}:{user_id}"

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self._path):
            return {}
        with open(self._path) as fh:
            return json.load(fh)

    def _save(self, data: dict[str, Any]) -> None:
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self._path, "w") as fh:
            json.dump(data, fh)
