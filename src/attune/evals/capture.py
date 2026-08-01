"""``attune eval capture`` (build prompt 27, task 1): turn a decided
(edited/rejected) decision-ledger row into a redacted regression case file
under ``evals/cases/``.

**Explicit and local, never automatic.** This is a standalone CLI step an
operator runs deliberately — it is not wired into ``resume_workflow`` or any
other production write path, so no principal's mail is ever harvested into
a checked-in fixture as a side effect of ordinary use.

**Where the text comes from.** The decision ledger
(``orchestrator.ledger``) and the audit log are both content-free by
construction — neither stores a draft body or a diff (see ``ledger.py``'s
own module docstring). The only place the actual proposed/sent text still
exists after a decision is the LangGraph checkpointer, keyed by the same
``thread_id`` the ledger row carries (``app.py`` wires a durable
``SqliteSaver`` in production). ``state_lookup`` is an injected
``Callable[[str], dict | None]`` so this module never depends on LangGraph's
checkpoint internals directly — production wiring
(``cli/eval_cmd.py::_checkpoint_state_lookup``) reads a real checkpoint;
tests inject a plain dict lookup. A thread whose checkpoint has already been
pruned (or was never durable) yields ``None`` and is silently skipped —
capture is best-effort by nature, and a fixture the operator can still
review by hand should never be a hard requirement.

**Idempotent by filesystem, not by a new ledger column.** A case's
``case_id`` is its ``proposal_id``; a re-run of ``attune eval capture``
skips any proposal that already has a file under ``cases_dir`` rather than
re-harvesting it, so running the command repeatedly (e.g. on a schedule an
operator sets up themselves) never overwrites a case a human may have
already hand-edited or redacted further.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Callable, Sequence

from ..orchestrator.ledger import DecisionLedger, LedgerRow
from .schema import NO_REPLY_GOLD, CaseKind, EvalCase, redact

StateLookup = Callable[[str], dict[str, Any] | None]


def build_case(row: LedgerRow, state: dict[str, Any]) -> EvalCase | None:
    """Build one redacted :class:`EvalCase` from a decided ledger row and
    its checkpointed graph state, or ``None`` when there's nothing usable
    to compare (an EDIT row whose final text somehow never made it into the
    checkpoint)."""
    if row.decision not in ("edited", "rejected"):
        return None

    kind = CaseKind.EDIT if row.decision == "edited" else CaseKind.REJECT
    proposed = state.get("proposed_draft") or ""
    if kind is CaseKind.EDIT:
        gold = state.get("final_text")
        if not gold:
            return None
    else:
        gold = NO_REPLY_GOLD

    inputs = {
        "incoming_summary": redact(state.get("incoming_summary") or ""),
        "priority": row.triage_priority,
        "tier": row.sender_importance_tier,
    }
    return EvalCase(
        case_id=row.proposal_id,
        kind=kind,
        domain=row.domain,
        action=row.action,
        inputs=inputs,
        retrieved_context_ids=row.context_attribution.memory_ids,
        prompt_version=row.prompt_version,
        proposed_text=redact(proposed),
        gold_text=redact(gold),
        captured_at=row.decided_at or row.proposed_at,
    )


def run_eval_capture(
    *,
    ledger: DecisionLedger,
    state_lookup: StateLookup,
    cases_dir: str,
    since: datetime | None = None,
    out: Callable[[str], None] = print,
) -> int:
    written = 0
    skipped = 0
    rows: Sequence[LedgerRow] = ledger.rows(since=since)
    for row in rows:
        if row.decision not in ("edited", "rejected"):
            continue
        path = os.path.join(cases_dir, f"{row.proposal_id}.json")
        if os.path.exists(path):
            continue
        state = state_lookup(row.thread_id)
        if not state:
            skipped += 1
            continue
        case = build_case(row, state)
        if case is None:
            skipped += 1
            continue
        os.makedirs(cases_dir, exist_ok=True)
        with open(path, "w") as f:
            json.dump(case.to_json(), f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(path, 0o600)
        written += 1
    out(f"captured {written} case(s); skipped {skipped} (no checkpoint state, or nothing to compare)")
    return 0
