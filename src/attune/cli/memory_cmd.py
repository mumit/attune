"""``attune memory`` — see, correct, and teach memory from the terminal
(roadmap prompt 11). Same engine as the chat commands (memory/commands.py);
this surface just renders it.
"""

from __future__ import annotations

from typing import Any, Callable


def run_memory_list(
    *,
    query: str | None = None,
    store: Any = None,
    settings: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    from ..memory.commands import list_memories

    store, settings = _resolve(store, settings)
    listing = list_memories(store, user_id=settings.user_id, query=query)
    out(listing.text)
    return 0


def run_memory_forget(
    memory_id: str,
    *,
    yes: bool = False,
    store: Any = None,
    settings: Any = None,
    audit_log: Any = None,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> int:
    from ..memory.commands import forget_memory, resolve_memory

    store, settings = _resolve(store, settings)
    record = resolve_memory(store, user_id=settings.user_id, selector=memory_id)
    if record is None:
        out(f"No unique memory matches '{memory_id}' — run `attune memory list`.")
        return 1
    if not yes:
        answer = ask(f"Delete: “{record.text}”? [y/N]: ").strip().lower()
        if answer != "y":
            out("Kept.")
            return 0
    forget_memory(
        store, record, user_id=settings.user_id,
        audit_log=audit_log if audit_log is not None else _audit(settings),
    )
    out(f"Forgotten: “{record.text}”")
    return 0


def run_memory_remember(
    text: str,
    *,
    store: Any = None,
    settings: Any = None,
    audit_log: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    from ..memory.commands import remember_fact

    store, settings = _resolve(store, settings)
    remember_fact(
        store, user_id=settings.user_id, text=text,
        audit_log=audit_log if audit_log is not None else _audit(settings),
    )
    out(f"Remembered: “{text}”")
    return 0


def _resolve(store: Any, settings: Any):  # pragma: no cover - live path
    from ..config import Settings

    resolved_settings = settings or Settings.from_env()
    if store is None:
        from ..memory.mem0_store import Mem0Store, build_mem0_config

        store = Mem0Store(build_mem0_config())
    return store, resolved_settings


def _audit(settings: Any):  # pragma: no cover - live path
    from ..audit.log import JsonlAuditLog

    return JsonlAuditLog(settings.audit_log_path)
