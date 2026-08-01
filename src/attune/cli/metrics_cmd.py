"""``attune metrics`` — the north-star metric, with its mandatory coverage
denominator (build prompt 26, ``docs/plan-2026-h2.md`` P2).

Reads the local decision ledger (``orchestrator.ledger.SqliteDecisionLedger``)
and renders :func:`orchestrator.ledger.render_metrics_table` — a plain table,
no charts: edit burden, clean-approval rate, p50 time-to-decision, coverage
(always present, never separable from edit burden — see the ledger module's
docstring for why), undo rate, and escalation rate, sliced by domain and by
sender importance tier.
"""

from __future__ import annotations

from typing import Any, Callable


def run_metrics(
    *,
    window_days: int = 14,
    settings: Any = None,
    ledger: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    from ..config import Settings
    from ..orchestrator import SqliteDecisionLedger, render_metrics_table

    resolved_settings = settings or Settings.from_env()
    resolved_ledger = ledger or SqliteDecisionLedger(resolved_settings.ledger_db_path)

    rows = resolved_ledger.rows()
    out(render_metrics_table(rows, window_days=window_days))
    return 0
