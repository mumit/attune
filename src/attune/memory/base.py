"""The substrate-agnostic memory interface (design doc 2.2, 2.3).

Everything above this line in the stack talks to ``MemoryStore``; only the
implementations below it know whether the substrate is Mem0 or (later) Graphiti.
That boundary is the whole point: the design commits to Mem0 for v1 with a
planned migration to Graphiti once temporal "who owns what, as of when" queries
start to matter, and this interface is what makes that migration an
implementation swap rather than an API rewrite.

The interface is deliberately small — add / search / get_all / delete /
consolidate — matching the four primitives Mem0 exposes plus the scheduled
consolidation pass the design calls for.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Scope(str, Enum):
    """Memory scoping. Kept small; maps onto Mem0's user/agent/run ids.

    The assistant runs as one agent for one principal, so ``user_id`` is the
    principal and ``run_id`` optionally isolates a single workflow's episodic
    trace. Instance isolation separation is handled by running *separate
    deployments with separate stores* (design 4.7), not by scoping within one
    store — so cross-deployment leakage is impossible by construction.
    """

    USER = "user"
    AGENT = "agent"
    RUN = "run"


@dataclass
class MemoryRecord:
    """A single retrieved memory, normalized across substrates."""

    id: str
    text: str
    score: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Message:
    """A conversation turn fed to ``add`` for fact extraction."""

    role: str  # "user" | "assistant" | "system"
    content: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


class MemoryStore(ABC):
    """Abstract memory substrate. Implementations: Mem0Store (now), Graphiti (later)."""

    @abstractmethod
    def add(
        self,
        messages: list[Message] | str,
        *,
        user_id: str,
        metadata: dict[str, Any] | None = None,
        infer: bool = True,
    ) -> list[MemoryRecord]:
        """Store memory. With ``infer=True`` the substrate's LLM extracts
        discrete facts; with ``infer=False`` the raw text is stored verbatim
        (used for high-signal captures we don't want paraphrased)."""

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        user_id: str,
        limit: int = 8,
        min_score: float | None = None,
    ) -> list[MemoryRecord]:
        """Retrieve memories relevant to ``query``, best first."""

    @abstractmethod
    def get_all(self, *, user_id: str, limit: int = 100) -> list[MemoryRecord]:
        """List memories under a scope (admin / the browser correction UI)."""

    @abstractmethod
    def delete(self, memory_id: str) -> None:
        """Remove one memory by id (user correcting a wrong fact)."""

    def consolidate(
        self, *, user_id: str, audit_log: Any = None
    ) -> "ConsolidationReport":
        """Scheduled maintenance pass (design 2.2): dedupe near-identical
        memories and supersede stale facts rather than overwriting them.

        Default implementation is a no-op that reports nothing changed;
        substrates override with real logic. Runs on a schedule, not inline,
        and is routed to the most capable model because correctness here
        compounds over time. ``audit_log`` (optional) journals every applied
        mutation so "what did last night's pass do to my memory" is a
        query, not archaeology."""
        return ConsolidationReport(user_id=user_id, ran_at=_now())


@dataclass
class ConsolidationReport:
    """Outcome of a consolidation pass, for the audit log."""

    user_id: str
    ran_at: datetime
    merged: int = 0
    superseded: int = 0
    notes: list[str] = field(default_factory=list)
