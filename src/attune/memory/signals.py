"""Turning interaction signals into memories (design doc 2.2).

This is what makes Attune *learn* rather than merely *remember*. The design
names two high-value capture signals; this module turns each into a well-formed
``add`` with metadata that makes it retrievable and auditable later.

1. **Correction diffs.** When the user edits a draft before sending, the delta
   between what the assistant proposed and what actually went out is the single
   richest preference signal available — it's the user showing, not telling. We
   capture the before/after so future drafts can be conditioned on it.

2. **Implicit action signals.** Approved / edited / ignored / rejected are
   labels on the assistant's judgment. "Ignored this sender three times" and
   "always approves calendar holds before 10am" are learnable patterns; we
   record the raw signal and let consolidation find the pattern.

We store these with ``infer`` chosen deliberately per signal: correction diffs
are stored with light inference (we want the *preference* extracted, e.g.
"prefers shorter replies to external vendors"), whereas raw action signals are
stored verbatim (``infer=False``) so the consolidation pass sees ground truth
rather than a premature paraphrase.
"""

from __future__ import annotations

import difflib
from enum import Enum
from typing import Any

from .base import MemoryStore, Message


class ActionSignal(str, Enum):
    """Implicit feedback on an assistant proposal."""

    APPROVED = "approved"      # sent/executed as-is -> the proposal was right
    EDITED = "edited"          # changed then sent -> partial; see the diff
    IGNORED = "ignored"        # left untouched -> weak negative
    REJECTED = "rejected"      # explicitly dismissed -> strong negative


def _short_diff(before: str, after: str, max_lines: int = 40) -> str:
    """A compact unified diff of a correction, for storage and prompting."""
    diff = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        fromfile="proposed",
        tofile="sent",
        lineterm="",
        n=1,
    )
    lines = list(diff)[:max_lines]
    return "\n".join(lines)


def capture_correction(
    store: MemoryStore,
    *,
    user_id: str,
    domain: str,
    proposed: str,
    sent: str,
    context: str | None = None,
) -> list[Any]:
    """Record a draft-vs-sent correction as a preference signal.

    No-op if the text is unchanged (an approval, not a correction — record that
    via :func:`capture_action_signal` instead).
    """
    if proposed.strip() == sent.strip():
        return []

    diff = _short_diff(proposed, sent)
    # Light inference: we want the *preference* extracted, not the raw diff, so
    # future drafting can be conditioned on the pattern.
    messages = [
        Message(
            role="user",
            content=(
                f"When I edit a {domain} draft, learn my preference from the "
                f"change. Context: {context or 'n/a'}.\n"
                f"You proposed:\n{proposed}\n\nI sent:\n{sent}"
            ),
        )
    ]
    return store.add(
        messages,
        user_id=user_id,
        metadata={
            "signal": "correction",
            "domain": domain,
            "diff": diff,
        },
        infer=True,
    )


def capture_action_signal(
    store: MemoryStore,
    *,
    user_id: str,
    domain: str,
    signal: ActionSignal,
    summary: str,
    metadata: dict[str, Any] | None = None,
) -> list[Any]:
    """Record an approve/edit/ignore/reject signal verbatim (``infer=False``).

    Stored raw so the scheduled consolidation pass (design 2.2), running on the
    strong model, can find cross-signal patterns from ground truth rather than
    from an eagerly-paraphrased summary.
    """
    meta = {"signal": "action", "action": signal.value, "domain": domain}
    if metadata:
        meta.update(metadata)
    text = f"[{signal.value}] {domain}: {summary}"
    return store.add(text, user_id=user_id, metadata=meta, infer=False)
