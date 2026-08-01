"""Memory: capture / consolidate / retrieve (design doc 2.2, 2.3).

Substrate-agnostic interface (``base.MemoryStore``) with a Mem0 implementation
(``mem0_store.Mem0Store``) wired to the configured LLM gateway, and
capture-signal helpers (``signals``) that turn correction diffs and action
signals into memories. The Graphiti migration once planned here is dropped
(``docs/plan-2026-h2.md`` P1, ``docs/landscape-2026.md`` §5): bitemporal
``valid_from``/``valid_to``/``superseded_by`` metadata on ``MemoryRecord`` is
the 80% of that advantage that matters, without a second store.
"""

from .base import (
    ConsolidationReport,
    MemoryRecord,
    MemoryStore,
    Message,
)
from .mem0_store import Mem0Store, build_mem0_config
from .signals import (
    ActionSignal,
    capture_action_signal,
    capture_correction,
    frame_memory_text,
)

__all__ = [
    "MemoryStore",
    "MemoryRecord",
    "Message",
    "ConsolidationReport",
    "Mem0Store",
    "build_mem0_config",
    "ActionSignal",
    "capture_correction",
    "capture_action_signal",
    "frame_memory_text",
]
