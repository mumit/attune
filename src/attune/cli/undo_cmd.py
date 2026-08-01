"""``attune undo`` — undo a previously applied effect (build prompt 31, task 2).

The counterpart to every approval card's post-apply confirmation: when
``apply_confirmation`` reports ``undo_available`` (build prompt 30's registry
says this capability has a real ``compensate`` function and isn't
``irreversible``), it names this exact command with the effect id (the
workflow's own ``thread_id``, also the decision ledger's ``proposal_id``).

Built on :func:`orchestrator.undo.undo_effect` — this module is a thin CLI
wrapper (parse the effect id, resolve a real runtime, print an honest
result), never a second place the undo/freshness/audit logic is written.

``runtime_factory`` is injected (mirrors ``cli/run_cmd.py``) so tests can
supply a fake :class:`~runtime.Runtime` without a live Google/LangGraph
connection — the compensating action must run against the SAME
connector-bound capability registry the original apply used
(``runtime.build_runtime`` wires ``make_*_compensate_fn(connector)`` into
``app.registry``), never a bare, no-op ``build_app()``.
"""

from __future__ import annotations

from typing import Any, Callable


def run_undo(
    effect_id: str,
    *,
    runtime_factory: Callable[[], Any] | None = None,
    actor: str = "cli",
    out: Callable[[str], None] = print,
) -> int:
    from ..orchestrator import UndoError, undo_effect
    from ..runtime import build_runtime

    rt = (runtime_factory or build_runtime)()
    app = rt.app

    try:
        result = undo_effect(
            effect_id,
            graph=app.graph,
            registry=app.registry,
            ledger=app.ledger,
            audit_log=app.audit_log,
            user_id=app.settings.user_id,
            actor=actor,
        )
    except UndoError as exc:
        out(f"Cannot undo: {exc}")
        return 2

    out(
        f"Undone: {result.action} on {result.domain} "
        f"(effect {result.thread_id})"
    )
    return 0
