"""Deterministic, fully-offline fakes used ONLY for the CI regression-gate
snapshot (``evals/ci_gate.py`` and ``attune eval run --offline``) and for
local dry runs without a configured model gateway.

**These are not a quality signal.** They exist purely so the harness's own
plumbing — position-swap disagreement wiring, the coverage/gate math, the
per-scorer regression diff — can run identically and reproducibly on every
PR without secrets or network, per ``docs/plan-2026-h2.md`` P2's "offline by
default" constraint. Real quality numbers come from the live variant
(``.github/workflows/eval-live.yml``, scheduled/manual, mirroring
``memory-eval.yml``'s existing pattern), never from these fakes. See
``docs/decisions.md`` for the recorded rationale.
"""

from __future__ import annotations

from typing import Any, Callable


class _Message:
    def __init__(self, content: str):
        self.content = content


class _Choice:
    def __init__(self, content: str):
        self.message = _Message(content)


class _Response:
    def __init__(self, content: str):
        self.choices = [_Choice(content)]


class FunctionClient:
    """A chat client whose response is computed by an injected pure
    function of the outgoing messages — the same minimal legacy shape
    ``llm.create_chat_completion`` already falls back to
    (``chat_completions_create``), so no OpenAI SDK object needs mocking."""

    def __init__(self, responder: Callable[[list[dict[str, Any]]], str]):
        self._responder = responder

    def chat_completions_create(self, **kwargs: Any) -> _Response:
        return _Response(self._responder(kwargs["messages"]))


def _judge_responder(messages: list[dict[str, Any]]) -> str:
    """Deterministic, content-based (never slot-based) preference: the
    response sharing more words with the stated context wins. Being a pure
    function of content rather than of the A/B slot means this fake will
    never show position-swap disagreement — real disagreement measurement
    is exercised by ``tests/test_evals_judge.py``'s purpose-built,
    deliberately slot-biased fakes, not by this one."""
    user = messages[-1]["content"]
    context_part, _, rest = user.partition("RESPONSE A:\n")
    a_text, _, b_text = rest.partition("\n\nRESPONSE B:\n")
    context = context_part.replace("CONTEXT:\n", "").strip()
    context_tokens = set(context.lower().split())
    a_overlap = len(context_tokens & set(a_text.lower().split()))
    b_overlap = len(context_tokens & set(b_text.lower().split()))
    if a_overlap > b_overlap:
        return "WINNER: A"
    if b_overlap > a_overlap:
        return "WINNER: B"
    return "WINNER: TIE"


def deterministic_judge_client() -> FunctionClient:
    return FunctionClient(_judge_responder)


_URGENT_KEYWORDS = ("urgent", "asap", "immediately", "escalation", "deadline today")
_NOISE_KEYWORDS = ("unsubscribe", "newsletter", "no-reply", "automated notification")


def _triage_responder(messages: list[dict[str, Any]]) -> str:
    """A crude keyword classifier — deterministic content, no intelligence.
    Never used for a live report; see the module docstring."""
    body = messages[-1]["content"].lower()
    if any(k in body for k in _URGENT_KEYWORDS):
        priority = "URGENT"
    elif any(k in body for k in _NOISE_KEYWORDS):
        priority = "NOISE"
    else:
        priority = "ROUTINE"
    return f"PRIORITY: {priority}\nREASON: offline deterministic keyword match"


def deterministic_triage_client() -> FunctionClient:
    return FunctionClient(_triage_responder)


def _draft_responder(messages: list[dict[str, Any]]) -> str:
    """A fixed, templated reply that echoes the incoming summary back —
    deterministic candidate text for the edit-burden-proxy CI snapshot."""
    user = messages[-1]["content"]
    return f"Thanks for your message. Re: {user.strip()[:120]}"


def deterministic_draft_client() -> FunctionClient:
    return FunctionClient(_draft_responder)
