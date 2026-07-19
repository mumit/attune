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
import logging
from enum import Enum
from typing import Any

from .base import MemoryStore, Message

logger = logging.getLogger(__name__)


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


#: Security finding F6 (SEC-605, Info): correction-derived memories touched
#: untrusted content (the diff is computed from a draft whose input was an
#: attacker-controlled email/chat body) and explicit teaching did not. At
#: RETRIEVAL time — the draft ``retrieve`` node, triage's past-reactions
#: garnish, and the conversational fallback — nothing previously
#: distinguished the two, so a memory whose provenance traced back to a
#: successful prompt-injection-into-a-draft would read exactly like a fact
#: the principal deliberately taught (pinned by
#: ``tests/test_signals.py::test_adversarial_two_stage_correction_provenance``).
#: These suffixes are PRESENTATION-LEVEL framing appended to a memory's text
#: right before it
#: enters a prompt — they never filter, drop, or reweight what's retrieved;
#: search/ranking/consolidation are unchanged. A human (or the model)
#: reading the annotation is meant to hold correction-derived preferences
#: more loosely than something the principal stated outright — the same
#: "provenance, not deletion" posture already used for untrusted mail/chat
#: content elsewhere in the prompt stack.
CORRECTION_ANNOTATION = " (learned from an edit — lower confidence than explicit teaching)"
EXPLICIT_ANNOTATION = " (explicitly taught)"


def frame_memory_text(text: str, metadata: dict[str, Any] | None) -> str:
    """Annotate one retrieved memory's text with its provenance, if known.

    Driven entirely by the ``signal`` key ``capture_correction``/
    ``remember_fact`` already stamp onto stored metadata — no new storage,
    no new field. Records that predate this metadata (or whose ``signal``
    is anything else — ``"action"``, ``"consolidated"``, missing) render
    byte-identical to before: this is additive framing, not a schema
    requirement. Call this at every site that turns a retrieved
    ``MemoryRecord`` into prompt text, not once centrally, because each site
    already has its own trust framing (untrusted-mail block, trusted
    past-reactions block, etc.) that this annotation must sit inside of.
    """
    signal = (metadata or {}).get("signal")
    if signal == "correction":
        return text + CORRECTION_ANNOTATION
    if signal == "explicit":
        return text + EXPLICIT_ANNOTATION
    return text


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
    importance_profile: Any = None,
    sender: str | None = None,
) -> list[Any]:
    """Record an approve/edit/ignore/reject signal verbatim (``infer=False``).

    Stored raw so the scheduled consolidation pass (design 2.2), running on the
    strong model, can find cross-signal patterns from ground truth rather than
    from an eagerly-paraphrased summary.

    Learning is one behavior with two stores (Phase 1, ``docs/future-state.md``):
    the same implicit-feedback event that feeds the soft memory search here
    also feeds the deterministic, inspectable per-sender profile in
    ``orchestrator/importance.py``. When both ``importance_profile`` (an
    :class:`~orchestrator.importance.ImportanceProfile`) and ``sender`` are
    given, the signal is additionally recorded there. Absent either, this
    function's memory-write behavior is unchanged — every existing caller
    that doesn't know about the profile keeps working untouched. A profile
    write failure is logged and swallowed: the importance profile is a
    fast-acting *addition* to learning, and it must never be able to break
    the memory write that everything else already depends on.
    """
    meta = {"signal": "action", "action": signal.value, "domain": domain}
    if metadata:
        meta.update(metadata)
    text = f"[{signal.value}] {domain}: {summary}"
    result = store.add(text, user_id=user_id, metadata=meta, infer=False)
    if importance_profile is not None and sender:
        try:
            importance_profile.record_signal(sender, signal)
        except Exception:  # noqa: BLE001 — profile write must never break memory
            logger.warning(
                "importance profile record_signal failed for sender=%s", sender,
                exc_info=True,
            )
    return result
