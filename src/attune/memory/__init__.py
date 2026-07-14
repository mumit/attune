"""Memory: capture / consolidate / retrieve (design doc 2.2, 2.3).

Substrate-agnostic interface (``base.MemoryStore``) with a Mem0 implementation
(``mem0_store.Mem0Store``) wired to the configured LLM gateway, and
capture-signal helpers (``signals``) that turn correction diffs and action
signals into memories. Migration path to Graphiti is an implementation swap
behind ``MemoryStore``, not an API change.
"""

from .base import (
    ConsolidationReport,
    MemoryRecord,
    MemoryStore,
    Message,
    Scope,
)
from .mem0_store import Mem0Store, build_mem0_config
from .signals import ActionSignal, capture_action_signal, capture_correction

__all__ = [
    "MemoryStore",
    "MemoryRecord",
    "Message",
    "Scope",
    "ConsolidationReport",
    "Mem0Store",
    "build_mem0_config",
    "ActionSignal",
    "capture_correction",
    "capture_action_signal",
]
