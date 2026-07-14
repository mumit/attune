"""Google Chat channel (design doc 3.1) — a thin door onto the orchestrator.

Same "one brain, many doors" stance as the Slack channel: this module owns no
assistant logic. It renders briefs and approval cards, and translates button
clicks into ``Command(resume=...)`` on the paused LangGraph workflow.

Transport contract:
- **Outbound (proactive):** an injected ``send_fn(space, payload)`` calls the
  Chat REST API on our behalf. The caller owns the credential and service
  lifecycle, keeping this class free of Google auth dependencies.
- **Inbound (card interactions):** button clicks arrive as HTTP POST events at
  the thin republisher outside this process (rule 5 — no inbound port on the
  credential-holding process). The republisher decodes the payload and calls
  ``handle_interaction(event)``; the return value is forwarded as the HTTP 200
  body back to Google Chat.

``send_fn`` and ``resume_fn`` are injected so the entire channel is testable
without credentials, a network call, or a live Chat workspace.

Interaction event shape (Google Chat CARD_CLICKED):
    {
      "type": "CARD_CLICKED",
      "action": {
        "actionMethodName": "<function>",   # e.g. "attune_approve"
        "parameters": [{"key": "thread_id", "value": "<tid>"}]
      }
    }
"""

from __future__ import annotations

from typing import Any, Callable

from ..ingestion.chat_interactions import decode_chat_interaction
from .gchat_cards import (
    ACTION_EDIT,
    approval_card,
    brief_card,
)


class GoogleChatChannel:
    """Wires an injected send function to the orchestrator for Google Chat.

    Args:
        graph: a compiled draft-and-approve graph. Used only by the default
            ``resume_fn``; inject a fake for tests.
        resume_fn: callable(thread_id, decision, text) -> resumes the graph.
            Defaults to a ``Command(resume=...)`` invoke against ``graph``.
        send_fn: callable(space, payload) -> sends a Chat message. ``space``
            is a resource name like ``"spaces/AAAA…"``. Inject a fake for
            tests; use ``make_chat_send_fn(credentials)`` in production.
    """

    def __init__(
        self,
        *,
        graph: Any = None,
        resume_fn: Callable[[str, str, str | None], Any] | None = None,
        send_fn: Callable[[str, dict[str, Any]], Any] | None = None,
    ):
        self._graph = graph
        self._resume = resume_fn or self._default_resume
        self._send = send_fn or _no_send_fn

    # --- public surface ----------------------------------------------------

    def post_brief(self, space: str, brief: Any) -> None:
        """Post a morning brief to a Chat space."""
        self._send(
            space,
            brief_card(
                summary=brief.summary,
                unread_count=brief.unread_count,
                event_count=brief.event_count,
            ),
        )

    def post_approval(
        self,
        space: str,
        *,
        thread_id: str,
        domain: str,
        proposed_draft: str,
        rationale: list[str] | None = None,
        title: str | None = None,
    ) -> None:
        """Post a draft-approval card to a Chat space for a paused workflow.
        ``title`` overrides the header so a nudge reads as a nudge."""
        self._send(
            space,
            approval_card(
                thread_id=thread_id,
                domain=domain,
                title=title,
                proposed_draft=proposed_draft,
                rationale=rationale,
            ),
        )

    def post_text(self, space: str, text: str) -> None:
        """Post a plain-text message — conversational Q&A replies, which have
        no card layout (contrast ``post_brief``/``post_approval``)."""
        self._send(space, {"text": text})

    def handle_interaction(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """Process a decoded CARD_CLICKED interaction event **synchronously**.

        Kept for tests and any direct in-process usage; production instead
        goes through the async path (the republisher forwards approve/reject
        clicks over Pub/Sub to ``dispatcher.handle_chat_interaction`` — see
        ``docs/decisions.md``), because resuming the graph needs the
        checkpointer/memory, which the public-facing republisher must never
        hold (rule 5). This method still handles the edit button directly
        either way, since opening a dialog never touches the graph.

        Returns a Chat response payload (sent back as the HTTP 200 body), or
        ``None`` if the event is not a recognized action.
        """
        if event.get("type") != "CARD_CLICKED":
            return None

        action = event.get("action", {})
        fn = action.get("actionMethodName", "")

        if fn == ACTION_EDIT:
            # Dialog-open never touches the graph: answer synchronously with
            # the edit dialog, prefilled from the card echoed in the event.
            thread_id = _get_param(action, "thread_id")
            if not thread_id:
                return None
            from .gchat_cards import edit_dialog, extract_draft_from_card_event

            return edit_dialog(
                thread_id=thread_id,
                proposed_draft=extract_draft_from_card_event(event) or "",
            )

        interaction = decode_chat_interaction(event)
        if interaction is None:
            return None

        result = self._resume(
            interaction.thread_id, interaction.decision, interaction.text
        )
        from ..orchestrator import apply_confirmation

        return {"text": apply_confirmation(interaction.decision, result)}

    # --- internals ---------------------------------------------------------

    def _default_resume(
        self, thread_id: str, decision: str, text: str | None
    ) -> Any:
        from ..orchestrator import resume_workflow

        return resume_workflow(self._graph, thread_id, decision, text)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_param(action: dict[str, Any], key: str) -> str | None:
    for p in action.get("parameters", []):
        if p.get("key") == key:
            return p.get("value")
    return None


def _no_send_fn(space: str, payload: dict[str, Any]) -> None:
    raise RuntimeError(
        "GoogleChatChannel: no send_fn provided. "
        "Pass send_fn=make_chat_send_fn(credentials) or inject a fake for tests."
    )


def make_chat_send_fn(credentials: Any) -> Callable[[str, dict[str, Any]], Any]:
    """Return a send_fn backed by the Google Chat REST API.

    Lazily imports google-api-python-client so the channel loads without it.
    Use this in the real app assembly::

        ch = GoogleChatChannel(send_fn=make_chat_send_fn(creds), ...)

    The returned callable is thread-safe for the same reasons as the other
    Google API services in this codebase (built once, called many times).
    """
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise ImportError(
            "make_chat_send_fn requires google-api-python-client. "
            "`pip install google-api-python-client`."
        ) from exc
    service = build("chat", "v1", credentials=credentials)

    def send(space: str, payload: dict[str, Any]) -> Any:
        return (
            service.spaces().messages().create(parent=space, body=payload).execute()
        )

    return send
