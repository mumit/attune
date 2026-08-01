"""Triage: urgent vs. routine vs. noise (design doc 1.2, 4.2).

Design 4.2 calls this out as one of the small, single-purpose graphs
("a triage graph (per incoming email/message)"). In practice it doesn't need
a LangGraph graph at all: like `brief.py`, it has no human-in-the-loop
interrupt to checkpoint around — it's one cheap, stateless classification
call (`Task.CLASSIFY` → Haiku 4.5) that decides whether the rest of the
pipeline should even run. A plain function is the simplest thing that
satisfies the design intent; see `docs/decisions.md` for the same reasoning
already applied to `brief.py`.

v2 (roadmap prompt 14) adds design 1.2's "your past reactions" signal: when
a memory store and the sender are available, one narrow search pulls up to
three captured-reaction lines into the prompt — the user's own behavior
(trusted, from memory), kept visually separate from the thread content,
which stays UNTRUSTED-framed. Still one cheap CLASSIFY call; the memory
search adds retrieval, not a second model call. Absent either argument,
behavior is byte-identical to v1 (the dispatcher's default path passes
both; direct callers without a store lose nothing).

v3 (Phase 1 of ``docs/future-state.md``, gap G4) adds a second, DETERMINISTIC
adjustment on top of the model call: when an ``importance_profile`` (an
``orchestrator.importance.ImportanceProfile``) and ``sender`` are given, the
principal's own recorded tier for that sender can move the model's priority
by exactly one step, in one direction only per tier:

- **LOW-tier senders demote**: URGENT -> ROUTINE, ROUTINE -> NOISE. This is
  the Phase 1 exit criterion made literal: a newsletter ignored three times
  in a row is NOISE the same day, no nightly consolidation needed.
- **HIGH-tier senders promote, but only NOISE -> ROUTINE.** This is a
  deliberate asymmetry, not an oversight: HIGH never promotes to URGENT.
  Urgency is a judgment about the CONTENT of this particular message; the
  importance profile is a judgment about the SENDER's track record. Letting
  a good track record fabricate same-day urgency the model itself didn't
  see would be the profile inventing facts about the current message. What
  the profile legitimately protects against is an important sender's mail
  being silently dropped as noise — hence NOISE -> ROUTINE is as far as it
  goes.
- **NORMAL tier never changes anything.**

This adjustment is intentionally different from the soft memory-reaction
garnish above in one respect: it DOES apply even when the model's own
response failed to parse (see ``_parse_triage_response``'s ROUTINE
fallback). That is not a contradiction of the "memory must never change the
failure default" rule below — the reaction garnish is retrieved, unverified
context fed INTO a model call whose failure we must not compound; the
importance profile is the principal's own already-recorded, deterministic
state (a pin, or a counted run of ignores/approvals) — the same class of
trusted input the autonomy gate already treats as authoritative, not a
second opinion riding on top of a call that just failed. A LOW-pinned
newsletter whose classification happened to fail parsing should still end
up NOISE, the same as if parsing had succeeded.

Every adjustment is audited: :class:`TriageResult` keeps ``base_priority``
(what the model said) alongside the effective ``priority`` and an
``adjusted`` flag, and the appended half of ``reason`` names the profile's
own grounded justification (``TierAssessment.reason``) — so "why did this
get demoted" is answerable without reading code.

The one thing this module decides is whether drafting happens at all —
`dispatcher.handle_gmail_notification` skips the draft-approve graph entirely
for threads classified as NOISE. It does NOT decide anything about autonomy
or take any write action (no auto-labeling, no auto-archiving): that would be
a new autonomous write path outside the existing per-(action,domain) autonomy
gate (rule 3), which is out of scope here.

Hosted seam (``docs/future-state.md`` Phase 5 item 1, gap G18): every
dependency of :func:`triage_thread` above — ``client`` (chat-completions),
``store``/``importance_profile`` (protocols), ``now`` implicitly via caller-
supplied timestamps — is already injected, so a hosted executor can call
this exact function unchanged, passing
``attune.hosted.intelligence.PostgresImportanceProfile`` as
``importance_profile``. No hosted-specific triage code is needed; this
module has nothing local-only left to extract.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from ..llm import (
    ModelCapabilities,
    Task,
    call_kwargs,
    call_with_retry,
    create_chat_completion,
    model_for,
    resolve_capabilities,
)
from ..memory.signals import UNTRUSTED_FIELD_NOTE, UNTRUSTED_FIELD_OPEN, frame_memory_text
from ..prompts import PROMPT_TRIAGE, render_system_message
from .importance import ImportanceTier

logger = logging.getLogger(__name__)

# Structured-output contract for PROMPT_TRIAGE (build prompt 28, task 4):
# declared to the gateway only when ATTUNE_MODEL_SUPPORTS_STRUCTURED_OUTPUT
# is set. The two-line PRIORITY:/REASON: text contract is unchanged and
# stays the fallback parse path either way (_parse_triage_response tries
# JSON first, then falls back to the line parse) -- a malformed response
# under either path still yields the fail-closed ROUTINE default.
_TRIAGE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "priority": {"type": "string", "enum": ["URGENT", "ROUTINE", "NOISE"]},
        "reason": {"type": "string"},
    },
    "required": ["priority", "reason"],
    "additionalProperties": False,
}


class Priority(str, Enum):
    URGENT = "urgent"
    ROUTINE = "routine"
    NOISE = "noise"


@dataclass
class TriageResult:
    """One triage decision.

    ``priority`` is the EFFECTIVE tier — what the dispatcher should act on.
    ``base_priority`` is what the model itself classified, before any
    importance-profile adjustment; it defaults to ``priority`` (via
    ``__post_init__``) so every existing call site that builds
    ``TriageResult(priority, reason)`` directly — tests, injected
    ``triage_fn`` overrides — keeps working unchanged and unadjusted.
    ``adjusted`` is True only when the profile actually moved the tier.
    """

    priority: Priority
    reason: str
    base_priority: Priority | None = None
    adjusted: bool = False

    def __post_init__(self) -> None:
        if self.base_priority is None:
            self.base_priority = self.priority


def triage_thread(
    client: Any,
    incoming_summary: str,
    *,
    store: Any = None,
    sender: str | None = None,
    user_id: str = "me",
    importance_profile: Any = None,
    trusted_context: str | None = None,
    min_score: float | None = None,
    now: datetime | None = None,
    capabilities: ModelCapabilities | None = None,
) -> TriageResult:
    """Classify one incoming thread as URGENT, ROUTINE, or NOISE.

    ``client`` uses the OpenAI-compatible Chat Completions surface; incoming content is framed as
    UNTRUSTED at the prompt boundary, same discipline as the draft node.
    When both ``store`` (a MemoryStore) and ``sender`` are given, up to
    three captured past-reaction lines are added as trusted context —
    letting repeated ignores/rejections of a sender inform the call.
    Parsing failures fall back to ROUTINE — the safe default, since ROUTINE
    still goes through drafting and human approval downstream, whereas
    defaulting to NOISE would silently drop real mail on a malformed model
    response. Memory input must never change that failure default.

    When both ``importance_profile`` and ``sender`` are given, the
    principal's own recorded tier for that sender may additionally adjust
    the result by one step (module docstring's v3 section has the full
    rules and the asymmetry rationale) — this adjustment DOES apply on top
    of the parse-failure default, unlike the soft memory garnish above.
    Profile failures fall back to the unadjusted result; a broken profile
    read must never break triage. ``now`` (hermetic-clock discipline,
    ``docs/decisions.md``'s P0 repair entry) is threaded through to the
    profile's ``assess`` call so the probation rule
    (``importance.PROBATION_DAYS``) evaluates against the caller's clock,
    not a bare ``datetime.now()`` a test can't freeze; defaults to the real
    wall clock via ``assess`` itself when omitted.

    ``trusted_context``, when given, is appended to the SYSTEM prompt —
    the same placement as the past-reactions garnish — never to the
    untrusted user blob. It exists for provider facts trusted code computed
    from event metadata (e.g. "this message @mentions the principal"): a
    fact riding inside the untrusted blob could be forged by a sender
    simply typing the same sentence, whereas nothing a sender writes can
    reach the system prompt. Callers must pass only text they constructed
    themselves, never content from the message.
    """
    volatile = ""
    reactions = _past_reactions(store, sender, user_id, min_score=min_score)
    if reactions:
        volatile += (
            "\n\nPAST REACTIONS (the user's own captured behavior toward this "
            "sender — trusted context, weigh it):\n" + reactions
        )
        if UNTRUSTED_FIELD_OPEN in reactions:
            # A captured action signal's sender/subject is attacker-
            # influenced (build prompt 25, task 1) even though the capture
            # event itself is trusted — see memory/signals.py's
            # UNTRUSTED_FIELD_NOTE and frame_memory_text's module docstring
            # for the same provenance discipline applied elsewhere.
            volatile += "\n\n" + UNTRUSTED_FIELD_NOTE
    if trusted_context:
        volatile += (
            "\n\nPROVIDER FACTS (computed by trusted code from event "
            "metadata, not from the message content — weigh them):\n"
            + trusted_context
        )
    caps = capabilities or resolve_capabilities()
    kwargs: dict[str, Any] = call_kwargs(caps)
    if caps.supports_structured_output:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "triage_result", "schema": _TRIAGE_JSON_SCHEMA, "strict": True},
        }
    resp = call_with_retry(
        lambda: create_chat_completion(
            client,
            model=model_for(Task.CLASSIFY),
            messages=[
                render_system_message(PROMPT_TRIAGE.stable_prefix, volatile, capabilities=caps),
                {"role": "user", "content": f"[UNTRUSTED mail]\n{incoming_summary}"},
            ],
            **kwargs,
        ),
        capabilities=caps,
    )
    result = _parse_triage_response(resp.choices[0].message.content)
    return _apply_importance_adjustment(result, importance_profile, sender, now=now)


def _past_reactions(
    store: Any, sender: str | None, user_id: str, *, min_score: float | None = None
) -> str:
    """Up to three short reaction lines for this sender, or "". Retrieval
    failures are silently empty — memory garnish must never break triage.

    Each line is provenance-framed (security finding F6, SEC-605) before it
    joins the trusted PAST REACTIONS block above: a reaction captured from
    an edited-then-approved draft is annotated as lower-confidence than
    explicit teaching, same as everywhere else memory reaches a prompt. The
    annotation travels WITH the line inside the trusted block — it does not
    move the line out of it; the block's own trust framing (the user's own
    captured behavior) is unchanged."""
    if store is None or not sender:
        return ""
    try:
        records = store.search(
            f"reactions to mail from {sender}",
            user_id=user_id,
            limit=3,
            min_score=min_score,
        )
    except Exception:  # noqa: BLE001
        return ""
    return "\n".join(
        f"- {frame_memory_text(r.text[:160], getattr(r, 'metadata', None))}"
        for r in records[:3]
    )


def _apply_importance_adjustment(
    result: TriageResult,
    importance_profile: Any,
    sender: str | None,
    *,
    now: datetime | None = None,
) -> TriageResult:
    """Apply the deterministic per-sender tier adjustment (module docstring,
    v3) on top of a model classification. Returns ``result`` unchanged when
    there's no profile/sender, when the tier is NORMAL, or when the
    directional rule for this tier doesn't apply to the current priority
    (e.g. a LOW-tier sender's NOISE stays NOISE — there's nothing lower to
    demote to)."""
    if importance_profile is None or not sender:
        return result

    try:
        assessment = importance_profile.assess(sender, now=now)
    except Exception:  # noqa: BLE001 — a broken profile must never break triage
        logger.warning(
            "importance profile assess failed for sender=%s", sender, exc_info=True
        )
        return result

    if assessment.tier == ImportanceTier.LOW and assessment.probation:
        # LOW-absorbing-state recovery (build prompt 25, task 2): the
        # sender's run has gone stale (importance.PROBATION_DAYS+ since the
        # last recorded signal), so this ONE message is let through with the
        # model's own classification unadjusted, rather than compounding a
        # freeze that would otherwise only ever heal via DECAY_DAYS or a
        # manual pin. Whatever the human (or the ignore-sweep) does next
        # records a fresh signal, which resets this clock.
        return result

    new_priority: Priority | None = None
    verb: str | None = None
    if assessment.tier == ImportanceTier.LOW:
        if result.priority == Priority.URGENT:
            new_priority, verb = Priority.ROUTINE, "demoted"
        elif result.priority == Priority.ROUTINE:
            new_priority, verb = Priority.NOISE, "demoted"
    elif assessment.tier == ImportanceTier.HIGH:
        if result.priority == Priority.NOISE:
            new_priority, verb = Priority.ROUTINE, "promoted"

    if new_priority is None:
        return result

    reason = (
        f"{result.reason}; {verb} from {result.priority.value}: {assessment.reason}"
        if result.reason
        else f"{verb} from {result.priority.value}: {assessment.reason}"
    )
    return TriageResult(
        priority=new_priority,
        reason=reason,
        base_priority=result.priority,
        adjusted=True,
    )


def _parse_triage_response(text: str) -> TriageResult:
    """JSON-first (the structured-output contract, when the gateway declared
    support), falling back to the original two-line ``PRIORITY:``/``REASON:``
    text parse either way — a gateway not declaring the capability never
    sends JSON here, so the JSON attempt below simply fails and falls
    through unchanged. Either path fails closed to ROUTINE (task 4's
    acceptance bar): a malformed or unparseable response of any shape never
    yields anything but the safe default."""
    parsed = _parse_triage_json(text)
    if parsed is not None:
        return parsed
    priority = Priority.ROUTINE
    reason = ""
    for line in (text or "").splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("PRIORITY:"):
            raw = stripped.split(":", 1)[1].strip().lower()
            try:
                priority = Priority(raw)
            except ValueError:
                pass
        elif upper.startswith("REASON:"):
            reason = stripped.split(":", 1)[1].strip()
    return TriageResult(priority=priority, reason=reason)


def _parse_triage_json(text: str) -> TriageResult | None:
    """The structured-output shape: ``{"priority": ..., "reason": ...}``.
    ``None`` on anything that isn't exactly that shape — the caller falls
    back to the text parse rather than guessing."""
    try:
        obj = json.loads((text or "").strip())
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    raw_priority, reason = obj.get("priority"), obj.get("reason")
    if not isinstance(raw_priority, str) or not isinstance(reason, str):
        return None
    try:
        priority = Priority(raw_priority.strip().lower())
    except ValueError:
        return None
    return TriageResult(priority=priority, reason=reason.strip())
