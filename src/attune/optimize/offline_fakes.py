"""Deterministic, fully-offline fakes for the optimizer's own tests and
local dry runs — the same posture ``evals.offline_fakes`` holds for the CI
regression-gate snapshot: **not a quality signal**, only plumbing (frontier
maintenance, merge wiring, promotion gating) exercised reproducibly without
secrets, network, or a real reflective model. Reuses ``evals.offline_fakes``'s
``FunctionClient`` shim rather than reimplementing the same OpenAI-response
shape.
"""

from __future__ import annotations

from typing import Any, Callable

from ..evals.offline_fakes import FunctionClient
from ..evals.schema import EvalCase

#: A candidate prefix containing this exact clause is "the reflected fix" in
#: :func:`prefix_sensitive_draft_fn_factory`'s deterministic world -- lets an
#: offline test prove the GEPA loop actually changes measured behavior when
#: (and only when) reflection proposes it, with no real model call.
MAGIC_MARKER = "Mirror the human's own phrasing wherever the incoming message already states it."


def deterministic_reflection_client() -> FunctionClient:
    """Always proposes the same fixed clause (:data:`MAGIC_MARKER`),
    regardless of which cases lost -- see module docstring: this is
    plumbing, never a quality signal."""

    def _responder(messages: list[dict[str, Any]]) -> str:
        user = messages[-1]["content"]
        current_prefix = ""
        if "CURRENT PREFIX:\n" in user:
            _, _, rest = user.partition("CURRENT PREFIX:\n")
            current_prefix, _, _ = rest.partition("\n\nLOSING CASES:")
        if MAGIC_MARKER in current_prefix:
            revised = current_prefix
        else:
            revised = current_prefix.rstrip() + "\n\n" + MAGIC_MARKER
        return (
            "DIAGNOSIS: drafts diverge from the human's own wording on losing cases.\n"
            f"REVISED_PREFIX:\n{revised}"
        )

    return FunctionClient(_responder)


def deterministic_merge_client() -> FunctionClient:
    """Deterministic merge: concatenates both prefixes' distinct lines,
    deduplicated in encounter order -- plumbing only, see module docstring."""

    def _responder(messages: list[dict[str, Any]]) -> str:
        user = messages[-1]["content"]
        _, _, rest = user.partition("PREFIX A")
        a_block, _, rest2 = rest.partition("\n\nPREFIX B")
        _, _, b_block = rest2.partition("):\n")
        a_text = a_block.partition("):\n")[2].strip()
        b_text = b_block.strip()
        seen: list[str] = []
        for line in (a_text + "\n" + b_text).splitlines():
            if line.strip() and line not in seen:
                seen.append(line)
        return "MERGED_PREFIX:\n" + "\n".join(seen)

    return FunctionClient(_responder)


#: The MIPRO analogue of :data:`MAGIC_MARKER`, for the ``triage`` prompt's
#: offline fakes below.
TRIAGE_MAGIC_MARKER = (
    "Use exact keyword matching: urgent/deadline/asap -> URGENT; "
    "unsubscribe/newsletter/automated -> NOISE; else ROUTINE."
)


def deterministic_instruction_proposer_client() -> FunctionClient:
    """A MIPRO instruction-proposer with no real intelligence -- always
    proposes the one fixed candidate containing :data:`TRIAGE_MAGIC_MARKER`,
    in the ``CANDIDATE 1:\\n<text>`` shape :func:`mipro._parse_candidates`
    expects. Plumbing only, see module docstring."""

    def _responder(messages: list[dict[str, Any]]) -> str:
        return f"CANDIDATE 1:\n{TRIAGE_MAGIC_MARKER}"

    return FunctionClient(_responder)


def prefix_sensitive_triage_fn_factory() -> Callable[[str], Callable[[Any], Any]]:
    """A ``triage_fn_factory`` (the shape ``mipro.run_mipro`` takes) whose
    classification depends on whether the given prefix contains
    :data:`TRIAGE_MAGIC_MARKER` -- with it, a deterministic keyword
    classifier; without it, always ``ROUTINE``. Lets an offline test prove
    MIPRO measurably improves triage accuracy end to end without any real
    model call."""
    from ..orchestrator.triage import Priority, TriageResult

    def factory(stable_prefix: str) -> Callable[[Any], Any]:
        marker_present = TRIAGE_MAGIC_MARKER in stable_prefix

        def triage_fn(case: Any) -> TriageResult:
            text = (case.incoming_summary or "").lower()
            if marker_present:
                if any(k in text for k in ("urgent", "deadline", "asap")):
                    priority = Priority.URGENT
                elif any(k in text for k in ("unsubscribe", "newsletter", "automated")):
                    priority = Priority.NOISE
                else:
                    priority = Priority.ROUTINE
            else:
                priority = Priority.ROUTINE
            return TriageResult(
                priority=priority, base_priority=priority, adjusted=False,
                reason="offline deterministic keyword match",
            )

        return triage_fn

    return factory


def prefix_sensitive_draft_fn_factory() -> Callable[[str], Callable[[EvalCase], str]]:
    """A ``candidate_fn_factory`` (the shape ``gepa.run_gepa`` takes) whose
    OUTPUT depends on whether the given prefix contains :data:`MAGIC_MARKER`
    -- with the marker, it echoes the case's own gold text (a perfect,
    zero-edit-distance draft); without it, a generic templated reply. Lets
    an offline test prove the optimizer measurably improves the north star
    end to end -- reflect proposes the marker, the next full scoring pass
    shows the improvement -- without any real model call."""

    def factory(stable_prefix: str) -> Callable[[EvalCase], str]:
        marker_present = MAGIC_MARKER in stable_prefix

        def candidate_fn(case: EvalCase) -> str:
            if marker_present and case.gold_text and not case.gold_text.startswith("(no reply"):
                return case.gold_text
            return "Thanks for reaching out, I'll take a look."

        return candidate_fn

    return factory
