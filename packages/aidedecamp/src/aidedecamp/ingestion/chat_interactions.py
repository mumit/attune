"""Decode Google Chat CARD_CLICKED interaction events (design 4.4, rule 5).

Card-click interactivity is the one Chat event type that can't follow the
Workspace-Events-API pull pattern the rest of Chat ingestion uses
(``chat_events.py``) — Google's interaction contract requires a *synchronous*
HTTP response, not an async Pub/Sub delivery. Resolving that tension (see
``docs/decisions.md``) means the public webhook endpoint (a thin republisher,
matching ``deploy/republisher/``) never touches the checkpointer, memory, or
any credential directly: it verifies the request is genuinely from Google,
then republishes the decoded event onto a Pub/Sub topic this process pulls
from — the same "notification is untrusted-origin input, never a direct
command" discipline already applied to Gmail/Calendar notifications.

APPROVE/REJECT and the edit dialog's SUBMIT go through this async path —
all three actually call ``Command(resume=...)``. EDIT's *initial* click never
touches the graph (it just opens a dialog — no state to protect), so the
republisher handles that synchronously and immediately; it is deliberately
not decodable here, so the async path can't accidentally resume on it.

Action name strings are intentionally duplicated from ``channels/blocks.py``
rather than imported — ``ingestion/`` doesn't depend on ``channels/``
anywhere else, and dispatcher-facing code deliberately never imports channel
code (design decision, see ``docs/decisions.md``, "Google Chat channel").
Kept in sync by ``test_chat_interactions.py``'s equality assertion, the same
technique already used to keep Slack's and Chat's own action names in sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Mirrors channels/blocks.py's ACTION_APPROVE/ACTION_REJECT/ACTION_EDIT_SUBMIT
# and gchat_cards.py's EDIT_DIALOG_FIELD (ACTION_EDIT — the dialog *open*
# click — is handled synchronously by the republisher itself, see module
# docstring, so it's deliberately not part of the decision map below).
_ACTION_APPROVE = "adc_approve"
_ACTION_REJECT = "adc_reject"
_ACTION_EDIT_SUBMIT = "adc_edit_submit"
_EDIT_DIALOG_FIELD = "adc_edit_text"

_DECISIONS = {
    _ACTION_APPROVE: "approved",
    _ACTION_REJECT: "rejected",
    _ACTION_EDIT_SUBMIT: "edited",
}


@dataclass
class ChatInteraction:
    thread_id: str
    decision: str  # "approved" | "rejected" | "edited"
    text: str | None = None  # the edited draft; only set for "edited"


def decode_chat_interaction(event: dict[str, Any]) -> ChatInteraction | None:
    """Parse a decoded CARD_CLICKED event into a resume-able interaction.

    Returns ``None`` for non-card-click events, a missing ``thread_id``, any
    action other than approve/reject/edit-submit (including the edit dialog's
    *open* click — see module docstring), or an edit submit with no edited
    text (nothing to resume with) — mirroring
    ``GoogleChatChannel.handle_interaction``'s existing filtering, so behavior
    is identical whether resumption happens synchronously (tests, or a direct
    in-process call) or asynchronously (production, via the republisher +
    Pub/Sub).
    """
    if event.get("type") != "CARD_CLICKED":
        return None

    action = event.get("action", {})
    fn = action.get("actionMethodName", "")
    decision = _DECISIONS.get(fn)
    if decision is None:
        return None

    thread_id = _get_param(action, "thread_id")
    if not thread_id:
        return None

    text: str | None = None
    if decision == "edited":
        text = _form_input(event, _EDIT_DIALOG_FIELD)
        if not text:
            return None

    return ChatInteraction(thread_id=thread_id, decision=decision, text=text)


def _form_input(event: dict[str, Any], field: str) -> str | None:
    """Read one text field from a dialog-submit event's ``common.formInputs``."""
    try:
        values = event["common"]["formInputs"][field]["stringInputs"]["value"]
    except (KeyError, TypeError):
        return None
    return values[0] if values else None


def _get_param(action: dict[str, Any], key: str) -> str | None:
    for p in action.get("parameters", []):
        if p.get("key") == key:
            return p.get("value")
    return None
