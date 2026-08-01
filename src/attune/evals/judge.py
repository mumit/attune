"""Pairwise judging (build prompt 27, task 2): compare a candidate draft
against the human's actual sent text (or, for a REJECT case, the fixed
"no reply" gold — see ``schema.NO_REPLY_GOLD``) and ask which a reader would
prefer. **Never** an absolute 1-5 score.

The 2026 judge literature this project is deliberately not ignoring
(``docs/plan-2026-h2.md`` P2): LLM judges are internally consistent but show
low inter-judge agreement and systematic length/style bias, and position
bias is *worst* on close calls — exactly the comparisons a release gate
actually needs to get right. Two structural responses, both enforced here in
code:

1. **Position is randomized on every comparison** (:func:`judge_pairwise`
   draws which slot the candidate lands in).
2. **Every comparison is run in BOTH orders**, so a genuine position-swap
   disagreement rate can be reported rather than assumed away. When the two
   orders disagree, the comparison is UNRESOLVED — excluded from the win-rate
   numerator rather than arbitrarily broken by a coin flip that would hide
   exactly the failure mode this module exists to surface.

``client`` is an injected OpenAI-compatible chat client, exactly like every
other model collaborator in this codebase (``llm.create_chat_completion``) —
offline tests inject a scripted fake (see ``tests/test_evals_judge.py``); a
live run injects a real gateway client.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from ..llm import Task, create_chat_completion, model_for

_SYSTEM = (
    "You are comparing two responses to the same message. Decide which "
    "response a reader would prefer overall. Never explain your reasoning.\n"
    "Respond with exactly one line:\n"
    "WINNER: <A|B|TIE>"
)


@dataclass(frozen=True)
class PairwiseResult:
    """One case's judged outcome. ``forward_winner``/``reversed_winner`` are
    each one of ``"candidate"``, ``"gold"``, ``"tie"`` — already translated
    out of the raw A/B slot the model saw, so callers never have to track
    which side was which. ``reported_winner`` is ``None`` exactly when the
    two orders disagree (a position-bias event) — the case contributes to
    ``disagreement_rate`` but not to a win/loss/tie count."""

    case_id: str
    forward_winner: str
    reversed_winner: str
    agrees: bool
    reported_winner: str | None


def _ask_winner(client: Any, *, context: str, a_text: str, b_text: str) -> str:
    """One judge call; returns ``"A"``, ``"B"``, or ``"TIE"``. A malformed
    response is treated as a TIE — never crash a judging pass on one bad
    completion, the same posture ``mem0_store.consolidate`` holds for a
    malformed consolidation response."""
    resp = create_chat_completion(
        client,
        model=model_for(Task.REASON),
        messages=[
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": (
                    f"CONTEXT:\n{context}\n\nRESPONSE A:\n{a_text}\n\n"
                    f"RESPONSE B:\n{b_text}"
                ),
            },
        ],
    )
    text = (resp.choices[0].message.content or "").strip().upper()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("WINNER:"):
            raw = stripped.split(":", 1)[1].strip()
            if raw.startswith("A"):
                return "A"
            if raw.startswith("B"):
                return "B"
            return "TIE"
    return "TIE"


def judge_pairwise(
    client: Any,
    *,
    case_id: str,
    context: str,
    candidate_text: str,
    gold_text: str,
    rng: random.Random | None = None,
) -> PairwiseResult:
    """Judge one case in both slot orders. ``rng`` decides only which order
    is called "forward" for this comparison (the randomization the module
    docstring's rule 1 requires) — both orders are always evaluated, so rule
    2 (report the disagreement) always has data to report."""
    rng = rng or random.Random()
    candidate_first = rng.random() < 0.5

    if candidate_first:
        forward_raw = _ask_winner(client, context=context, a_text=candidate_text, b_text=gold_text)
        forward_winner = {"A": "candidate", "B": "gold", "TIE": "tie"}[forward_raw]
        reversed_raw = _ask_winner(client, context=context, a_text=gold_text, b_text=candidate_text)
        reversed_winner = {"A": "gold", "B": "candidate", "TIE": "tie"}[reversed_raw]
    else:
        forward_raw = _ask_winner(client, context=context, a_text=gold_text, b_text=candidate_text)
        forward_winner = {"A": "gold", "B": "candidate", "TIE": "tie"}[forward_raw]
        reversed_raw = _ask_winner(client, context=context, a_text=candidate_text, b_text=gold_text)
        reversed_winner = {"A": "candidate", "B": "gold", "TIE": "tie"}[reversed_raw]

    agrees = forward_winner == reversed_winner
    return PairwiseResult(
        case_id=case_id,
        forward_winner=forward_winner,
        reversed_winner=reversed_winner,
        agrees=agrees,
        reported_winner=forward_winner if agrees else None,
    )
