"""The golden eval-case schema (build prompt 27, ``docs/plan-2026-h2.md`` P2,
task 1) — "a golden set that grows from real decisions".

An :class:`EvalCase` is a redacted, reviewable regression case derived from
one decided (edited or rejected) row in the decision ledger
(``orchestrator.ledger``). It carries the SAME shape the ledger already
computes attribution for — inputs, retrieved context ids, prompt version —
plus the human's actual sent text as gold (:mod:`capture` is where a case is
actually built; this module only defines the shape and the redaction pass
every case's text goes through before it is ever written to a checked-in
fixture).

Two kinds of case, matching the two decision outcomes that make a proposal a
useful regression:

- ``EDIT``: the human sent something different from the draft. ``gold_text``
  is what they actually sent — a real pairwise comparison target.
- ``REJECT``: the human sent nothing. There is no "sent text" to be gold for
  a REJECT case, so ``gold_text`` is the fixed :data:`NO_REPLY_GOLD` sentinel
  — pairwise judging still applies (a reader is asked whether the candidate
  draft is actually better than not replying at all), which is the correct
  generalization of "pairwise against what the human actually sent" to the
  case where what they sent was nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class CaseKind(str, Enum):
    EDIT = "edit"
    REJECT = "reject"


#: The fixed gold text for a REJECT case — see the module docstring.
NO_REPLY_GOLD = "(no reply — the human rejected the draft and sent nothing)"

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
_URL_RE = re.compile(r"https?://\S+")
_PHONE_RE = re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b")


def redact(text: str) -> str:
    """Best-effort scrub applied to every string an :class:`EvalCase` stores,
    before it is ever written to ``evals/cases/`` — email addresses, URLs,
    and US-shaped phone numbers become fixed placeholders.

    This is NOT a general PII scrubber (no name detection, no address
    detection) — it catches the mechanically-detectable identifiers that
    would otherwise turn a checked-in regression fixture into leaked
    correspondence. ``attune eval capture`` is explicit, opt-in, and local
    (see that module's docstring); a human is expected to review a case file
    before committing it, the same posture ``memory list``/`forget` already
    give a principal over what memory holds.
    """
    text = _EMAIL_RE.sub("[REDACTED-EMAIL]", text)
    text = _URL_RE.sub("[REDACTED-URL]", text)
    text = _PHONE_RE.sub("[REDACTED-PHONE]", text)
    return text


@dataclass(frozen=True)
class EvalCase:
    """One regression case: everything :func:`capture.build_case` could
    recover about a decided proposal, redacted. ``inputs`` deliberately
    holds only what a drafting/judging pass needs to reproduce the
    comparison (the redacted incoming summary, triage priority, sender
    tier) — never the sender/subject themselves, which is exactly the
    attacker-influenced substring ``memory.signals`` already fences
    elsewhere; a case file has no need to carry it at all."""

    case_id: str
    kind: CaseKind
    domain: str
    action: str
    inputs: dict[str, Any]
    retrieved_context_ids: tuple[str, ...]
    prompt_version: str | None
    proposed_text: str
    gold_text: str
    captured_at: datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "kind": self.kind.value,
            "domain": self.domain,
            "action": self.action,
            "inputs": self.inputs,
            "retrieved_context_ids": list(self.retrieved_context_ids),
            "prompt_version": self.prompt_version,
            "proposed_text": self.proposed_text,
            "gold_text": self.gold_text,
            "captured_at": self.captured_at.astimezone(timezone.utc).isoformat(),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "EvalCase":
        return cls(
            case_id=raw["case_id"],
            kind=CaseKind(raw["kind"]),
            domain=raw["domain"],
            action=raw["action"],
            inputs=dict(raw.get("inputs") or {}),
            retrieved_context_ids=tuple(raw.get("retrieved_context_ids") or ()),
            prompt_version=raw.get("prompt_version"),
            proposed_text=raw["proposed_text"],
            gold_text=raw["gold_text"],
            captured_at=datetime.fromisoformat(raw["captured_at"]),
        )
