"""The coverage guardrail for candidate prompt promotion (build prompt 36,
``docs/plan-2026-h2.md`` P10, task 5): "the optimizer will find that the
cheapest way to reduce edit burden is to propose less."

The decision ledger's own coverage metric (``orchestrator.ledger._coverage``)
is a PRODUCTION signal -- proposals over eligible items -- that doesn't exist
yet for a candidate prompt nobody has deployed. This module is the offline
proxy the optimizer scores a candidate against instead: over the golden set's
EDIT-kind cases (a human sent a real reply, so a substantive draft was the
correct behavior), what fraction did the candidate actually draft something
for, rather than going quiet?

Deliberately scoped to EDIT-kind cases only. A REJECT-kind case's gold is
"the human sent nothing" (``schema.NO_REPLY_GOLD``) -- declining there is
often the CORRECT behavior a human already confirmed, so counting it against
coverage would penalize an assistant for agreeing with its own principal.
The RLUF failure mode this guards against ("an assistant that drafts only the
easy replies and stays silent on the hard ones") is specifically about cases
where a reply was wanted; that is exactly what EDIT-kind cases are.
"""

from __future__ import annotations

from typing import Callable, Sequence

from ..evals.schema import CaseKind, EvalCase

#: A draft shorter than this (after stripping) is treated as non-substantive
#: -- a degenerate output ("N/A", ".", an empty string) rather than a real
#: attempt at a reply. Deliberately low: this is a coverage floor, not a
#: quality judgment (the pairwise judge already owns quality).
MIN_SUBSTANTIVE_CHARS = 10


def is_substantive(text: str | None) -> bool:
    return bool(text and len(text.strip()) >= MIN_SUBSTANTIVE_CHARS)


def compute_coverage_proxy(
    cases: Sequence[EvalCase],
    candidate_fn: Callable[[EvalCase], str],
) -> float | None:
    """Fraction of EDIT-kind cases for which ``candidate_fn`` produced a
    substantive draft. ``None`` when there are no EDIT-kind cases to measure
    -- absent coverage should never render as a misleading 0.0 or 1.0, the
    same "don't trust either way" posture ``evals.agreement.domain_gates``
    already holds for an unmeasured judge-agreement domain."""
    edit_cases = [c for c in cases if c.kind is CaseKind.EDIT]
    if not edit_cases:
        return None
    substantive = sum(1 for c in edit_cases if is_substantive(candidate_fn(c)))
    return substantive / len(edit_cases)
