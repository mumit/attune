"""Google Chat Cards v2 builders (design doc 3.1).

Pure functions that turn domain objects into Google Chat message payloads.
Kept separate from the channel wiring so they're testable without a Chat
connection and run without google-api-python-client installed.

The approval card is the Chat equivalent of Slack's approval_blocks(): it
shows the proposed draft with Approve / Edit / Reject buttons, each carrying
the LangGraph thread_id as an action parameter so the click can resume the
exact paused workflow.

Action function names are shared with blocks.py so the orchestrator layer
never needs to branch on channel type — both surfaces use the same string
identifiers for the same decisions.
"""

from __future__ import annotations

from typing import Any

# Reuse the same action name strings as the Slack blocks so the orchestrator
# doesn't branch on channel type. These become the 'function' field inside
# onClick.action in Chat cards, and appear as action.actionMethodName in the
# CARD_CLICKED interaction event.
from .blocks import ACTION_APPROVE, ACTION_EDIT, ACTION_EDIT_SUBMIT, ACTION_REJECT

# The dialog's text-input field name — where the edited draft comes back in
# the dialog-submit event's common.formInputs. Mirrored (not imported) in
# ingestion/chat_interactions.py and deploy/republisher/, pinned by tests.
EDIT_DIALOG_FIELD = "attune_edit_text"

__all__ = [
    "ACTION_APPROVE",
    "ACTION_EDIT",
    "ACTION_EDIT_SUBMIT",
    "ACTION_REJECT",
    "EDIT_DIALOG_FIELD",
    "brief_card",
    "approval_card",
    "edit_dialog",
    "extract_draft_from_card_event",
]


def brief_card(
    *, summary: str, unread_count: int, event_count: int
) -> dict[str, Any]:
    """Return a Google Chat cardsV2 message payload for the morning brief."""
    return {
        "cardsV2": [
            {
                "cardId": "attune_brief",
                "card": {
                    "header": {
                        "title": "Morning brief",
                        "subtitle": (
                            f"{unread_count} unread · {event_count} events today"
                        ),
                    },
                    "sections": [
                        {
                            "widgets": [
                                {"textParagraph": {"text": summary}},
                            ]
                        }
                    ],
                },
            }
        ]
    }


def approval_card(
    *,
    thread_id: str,
    domain: str,
    proposed_draft: str,
    rationale: list[str] | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Return a Google Chat cardsV2 message payload for a draft-approval request.

    ``thread_id`` is the LangGraph thread id of the paused workflow; it is
    stored as an action parameter in every button so the CARD_CLICKED event
    can resume the right graph.
    """

    def _btn(text: str, fn: str, *, color: dict[str, float] | None = None) -> dict:
        btn: dict[str, Any] = {
            "text": text,
            "onClick": {
                "action": {
                    "function": fn,
                    "parameters": [{"key": "thread_id", "value": thread_id}],
                }
            },
        }
        if color:
            btn["color"] = color
        return btn

    widgets: list[dict[str, Any]] = [
        {"textParagraph": {"text": proposed_draft}},
    ]
    if rationale:
        why = "\n".join(f"• {r}" for r in rationale[:3])
        widgets.append({"textParagraph": {"text": f"Based on: {why}"}})
    widgets.append(
        {
            "buttonList": {
                "buttons": [
                    _btn(
                        "Approve",
                        ACTION_APPROVE,
                        color={"red": 0.06, "green": 0.57, "blue": 0.14, "alpha": 1.0},
                    ),
                    _btn("Edit", ACTION_EDIT),
                    _btn(
                        "Reject",
                        ACTION_REJECT,
                        color={"red": 0.8, "green": 0.0, "blue": 0.0, "alpha": 1.0},
                    ),
                ]
            }
        }
    )

    return {
        "cardsV2": [
            {
                "cardId": f"attune_approval:{thread_id}",
                "card": {
                    "header": {
                        "title": title or f"Draft reply ({domain})",
                        "subtitle": "Approve before it goes out",
                    },
                    "sections": [{"widgets": widgets}],
                },
            }
        ]
    }


def edit_dialog(*, thread_id: str, proposed_draft: str) -> dict[str, Any]:
    """The edit dialog (Chat's modal equivalent), returned synchronously as
    the ``actionResponse`` to an Edit click.

    The submit button's action carries ``thread_id`` and fires
    ``ACTION_EDIT_SUBMIT`` — a real graph resume, so (unlike this dialog-open
    response) it goes through the async republisher → Pub/Sub path like
    approve/reject. The edited text comes back in the submit event's
    ``common.formInputs[EDIT_DIALOG_FIELD]``.
    """
    return {
        "actionResponse": {
            "type": "DIALOG",
            "dialogAction": {
                "dialog": {
                    "body": {
                        "sections": [
                            {
                                "header": "Edit draft",
                                "widgets": [
                                    {
                                        "textInput": {
                                            "name": EDIT_DIALOG_FIELD,
                                            "label": "Your reply",
                                            "type": "MULTIPLE_LINE",
                                            "value": proposed_draft,
                                        }
                                    },
                                    {
                                        "buttonList": {
                                            "buttons": [
                                                {
                                                    "text": "Save & apply",
                                                    "onClick": {
                                                        "action": {
                                                            "function": ACTION_EDIT_SUBMIT,
                                                            "parameters": [
                                                                {
                                                                    "key": "thread_id",
                                                                    "value": thread_id,
                                                                }
                                                            ],
                                                        }
                                                    },
                                                }
                                            ]
                                        }
                                    },
                                ],
                            }
                        ]
                    }
                }
            },
        }
    }


def extract_draft_from_card_event(event: dict[str, Any]) -> str | None:
    """Pull the proposed draft out of a CARD_CLICKED event's echoed message.

    Chat includes the clicked card in ``event["message"]["cardsV2"]``; the
    draft is the first ``textParagraph`` widget ``approval_card`` rendered.
    Returns ``None`` when the event carries no recognizable approval card.
    """
    try:
        cards = event["message"]["cardsV2"]
        widgets = cards[0]["card"]["sections"][0]["widgets"]
    except (KeyError, IndexError, TypeError):
        return None
    for w in widgets:
        text = (w.get("textParagraph") or {}).get("text")
        if text:
            return text
    return None
