"""Bounded natural-language planning for Slack and Google Chat.

The model chooses among a deliberately small set of read-only Workspace
operations.  It never receives a generic tool loop and it cannot authorize a
write: mutations continue to enter Attune through explicit, audited workflows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from .llm import (
    ModelCapabilities,
    Task,
    call_kwargs,
    call_with_retry,
    create_chat_completion,
    model_for,
    resolve_capabilities,
)
from .prompts import PROMPT_INTERACTION_PLAN, render_system_message

# Native tool-calling contract for PROMPT_INTERACTION_PLAN (build prompt 28,
# task 5) -- the planner's four-line INTENT/GMAIL_QUERY/START/END text
# contract, declared as a forced tool call when the gateway supports it.
# Fields are identical in name and meaning to the text contract so
# ``_plan_from_fields`` (below) parses either shape the same way; the
# deterministic keyword fallback in :func:`plan_interaction` overrides both
# paths identically and unchanged.
_PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "emit_plan",
        "description": "Emit the routing decision for this message.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["BRIEF", "MAIL", "CALENDAR", "WRITE", "GENERAL"],
                },
                "gmail_query": {
                    "type": "string",
                    "description": "Conservative Gmail search query, or NONE.",
                },
                "start": {
                    "type": "string",
                    "description": "ISO-8601 timestamp, or NONE.",
                },
                "end": {
                    "type": "string",
                    "description": "ISO-8601 timestamp, or NONE.",
                },
            },
            "required": ["intent", "gmail_query", "start", "end"],
            "additionalProperties": False,
        },
    },
}


class InteractionIntent(str, Enum):
    BRIEF = "brief"
    MAIL = "mail"
    CALENDAR = "calendar"
    WRITE = "write"
    GENERAL = "general"


@dataclass(frozen=True)
class InteractionPlan:
    intent: InteractionIntent
    gmail_query: str = ""
    start: datetime | None = None
    end: datetime | None = None


def plan_interaction(
    client: Any,
    text: str,
    *,
    timezone_name: str = "UTC",
    history: list[dict[str, str]] | None = None,
    now: datetime | None = None,
    capabilities: ModelCapabilities | None = None,
) -> InteractionPlan:
    """Classify an authenticated human message into one bounded operation.

    Parsing fails closed to deterministic read-only heuristics. A malformed
    model response can therefore lose convenience, but can never become a
    write or broaden a Workspace query without an explicit read intent.

    When the configured gateway declares ``supports_tools`` (build prompt
    28, task 5), the four-line INTENT/GMAIL_QUERY/START/END text contract is
    replaced by a forced call to the ``emit_plan`` tool (:data:`_PLAN_TOOL`)
    — same fields, same meaning, parsed by the same :func:`_plan_from_fields`
    either way. The capability-off path is byte-identical to before this
    parameter existed: no ``tools``/``tool_choice`` key is added, and the
    text parse runs exactly as it always has.
    """
    zone = ZoneInfo(timezone_name)
    resolved_now = now or datetime.now(zone)
    if resolved_now.tzinfo is None:
        resolved_now = resolved_now.replace(tzinfo=zone)
    local_now = resolved_now.astimezone(zone)
    fallback = _fallback_plan(text, zone=zone, now=local_now)
    context = _history_text(history or [])
    user = (
        f"LOCAL_NOW: {local_now.isoformat()}\n"
        f"TIMEZONE: {timezone_name}\n"
        f"RECENT_CONVERSATION (untrusted):\n{context or '(none)'}\n\n"
        f"CURRENT_MESSAGE:\n{text}"
    )
    caps = capabilities or resolve_capabilities()
    kwargs: dict[str, Any] = call_kwargs(caps)
    if caps.supports_tools:
        kwargs["tools"] = [_PLAN_TOOL]
        kwargs["tool_choice"] = {"type": "function", "function": {"name": "emit_plan"}}
    try:
        response = call_with_retry(
            lambda: create_chat_completion(
                client,
                model=model_for(Task.CLASSIFY),
                messages=[
                    render_system_message(PROMPT_INTERACTION_PLAN.stable_prefix, capabilities=caps),
                    {"role": "user", "content": user},
                ],
                **kwargs,
            ),
            capabilities=caps,
        )
        message = response.choices[0].message
        fields = _fields_from_tool_call(message) if caps.supports_tools else None
        if fields is None:
            fields = _fields_from_text(message.content or "")
        parsed = _plan_from_fields(fields, zone=zone, now=local_now)
        if parsed is not None:
            # Strong deterministic signals prevent a model from turning an
            # obvious read into memory-only chat, or an imperative mutation
            # into an executable read. The model still resolves richer sender,
            # Gmail-query, and date-range language.
            if fallback.intent == InteractionIntent.WRITE:
                return fallback
            if parsed.intent == InteractionIntent.GENERAL and fallback.intent != InteractionIntent.GENERAL:
                return fallback
            if parsed.intent == InteractionIntent.BRIEF and fallback.intent == InteractionIntent.CALENDAR:
                return fallback
            return parsed
    except Exception:  # noqa: BLE001 — deterministic fallback is the contract
        pass
    return fallback


def _fields_from_text(raw: str) -> dict[str, str]:
    """The original four-line ``KEY: value`` text contract, parsed into the
    same field shape :func:`_fields_from_tool_call` produces."""
    fields: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip().upper()] = value.strip()
    return fields


def _fields_from_tool_call(message: Any) -> dict[str, str] | None:
    """The ``emit_plan`` tool-call contract (build prompt 28, task 5),
    parsed into the exact same field shape :func:`_fields_from_text`
    produces — ``None`` on anything that isn't a well-formed call to that
    tool, so the caller falls back to a text parse of ``message.content``
    rather than guessing."""
    tool_calls = getattr(message, "tool_calls", None) or []
    for call in tool_calls:
        function = getattr(call, "function", None)
        if function is None or getattr(function, "name", None) != "emit_plan":
            continue
        try:
            args = json.loads(getattr(function, "arguments", "") or "")
        except ValueError:
            return None
        if not isinstance(args, dict):
            return None
        fields: dict[str, str] = {}
        for key, field_name in (
            ("intent", "INTENT"), ("gmail_query", "GMAIL_QUERY"),
            ("start", "START"), ("end", "END"),
        ):
            value = args.get(key)
            if isinstance(value, str):
                fields[field_name] = value
        return fields
    return None


def _plan_from_fields(
    fields: dict[str, str], *, zone: ZoneInfo, now: datetime
) -> InteractionPlan | None:
    try:
        intent = InteractionIntent(fields["INTENT"].lower())
    except (KeyError, ValueError):
        return None

    query = fields.get("GMAIL_QUERY", "")
    query = "" if query.upper() == "NONE" else _one_line(query)[:300]
    start = _parse_datetime(fields.get("START"), zone)
    end = _parse_datetime(fields.get("END"), zone)

    if intent == InteractionIntent.MAIL:
        query = query or "newer_than:7d"
    elif intent == InteractionIntent.CALENDAR:
        start, end = _bounded_window(start, end, now)
    return InteractionPlan(intent, query, start, end)


def _fallback_plan(text: str, *, zone: ZoneInfo, now: datetime) -> InteractionPlan:
    lower = text.lower()
    stripped = lower.strip()
    write_starts = (
        "draft ", "send ", "label ", "archive ", "delete ", "schedule ",
        "book ", "move ", "reschedule ", "cancel ", "create a meeting",
        "add a meeting",
    )
    if stripped.startswith(write_starts):
        return InteractionPlan(InteractionIntent.WRITE)

    overview_phrases = (
        "anything new", "what's new", "what is new", "what's on my plate",
        "what is on my plate", "needs my attention", "to report",
    )
    if any(word in lower for word in ("brief", "summary")) or any(
        phrase in lower for phrase in overview_phrases
    ):
        return InteractionPlan(InteractionIntent.BRIEF)

    mail_words = ("mail", "email", "inbox", "unread", "message", "replied", "reply")
    if any(word in lower for word in mail_words):
        query = "is:unread newer_than:7d" if "unread" in lower else "newer_than:7d"
        return InteractionPlan(InteractionIntent.MAIL, gmail_query=query)

    calendar_words = (
        "calendar", "meeting", "appointment", "agenda", "schedule", "free time",
        "event",
    )
    temporal_question = any(word in lower for word in ("today", "tomorrow")) and any(
        phrase in lower for phrase in ("what", "when", "do i have", "am i free")
    )
    if any(word in lower for word in calendar_words) or temporal_question:
        day = now.date() + timedelta(days=1 if "tomorrow" in lower else 0)
        start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
        if "morning" in lower:
            start = start.replace(hour=5)
            end = start.replace(hour=12)
        else:
            end = start + timedelta(days=1)
        return InteractionPlan(InteractionIntent.CALENDAR, start=start, end=end)

    if "morning" in lower:
        return InteractionPlan(InteractionIntent.BRIEF)

    return InteractionPlan(InteractionIntent.GENERAL)


def _parse_datetime(raw: str | None, zone: ZoneInfo) -> datetime | None:
    if not raw or raw.upper() == "NONE":
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=zone)
    return value


def _bounded_window(
    start: datetime | None, end: datetime | None, now: datetime
) -> tuple[datetime, datetime]:
    start = start or now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = end or (start + timedelta(days=1))
    if end <= start:
        end = start + timedelta(days=1)
    if end - start > timedelta(days=31):
        end = start + timedelta(days=31)
    return start, end


def _history_text(history: list[dict[str, str]]) -> str:
    lines = []
    for turn in history[-6:]:
        role = turn.get("role", "unknown")
        content = _one_line(turn.get("content", ""))[:500]
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _one_line(value: str) -> str:
    return " ".join((value or "").split())
