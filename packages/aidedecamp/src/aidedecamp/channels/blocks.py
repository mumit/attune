"""Slack Block Kit builders (design doc 3.1).

Pure functions that turn domain objects into Slack block payloads. Kept separate
from the app wiring so they're testable without a Slack connection and reusable
across Slack and (later) Google Chat cards. No side effects, no I/O.

The approval card is the visible form of the rung-2 loop: it shows the proposed
draft and offers Approve / Edit / Reject, each carrying the graph's thread_id so
the click can be routed back to resume the exact paused workflow.
"""

from __future__ import annotations

import json
from typing import Any

# action_ids the app listens for. Centralized so the app wiring and the card
# can't drift apart.
ACTION_APPROVE = "adc_approve"
ACTION_EDIT = "adc_edit"
ACTION_REJECT = "adc_reject"
# The edit dialog/modal *submit* action — distinct from ACTION_EDIT (which
# only opens the editor and never touches the graph). In Slack this is the
# modal's callback_id; in Chat it's the dialog submit button's function name
# (mirrored in ingestion/chat_interactions.py and deploy/republisher/,
# kept in sync by tests).
ACTION_EDIT_SUBMIT = "adc_edit_submit"

# Slack modal internals: where the edited text lives in the view_submission
# payload (view.state.values[<block_id>][<action_id>].value).
EDIT_MODAL_BLOCK_ID = "adc_edit_block"
EDIT_MODAL_INPUT_ID = "adc_edit_input"

# The draft is rendered as a Slack blockquote inside the approval card; the
# edit modal re-extracts it from there (the card itself is the single source
# of what the user saw and chose to edit).
_DRAFT_QUOTE_PREFIX = ">>> "


def brief_blocks(*, summary: str, unread_count: int, event_count: int) -> list[dict[str, Any]]:
    """Render the morning brief as Slack blocks."""
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Morning brief", "emoji": True},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*{unread_count}* unread · *{event_count}* events today",
                }
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
    ]


def approval_blocks(
    *,
    thread_id: str,
    domain: str,
    proposed_draft: str,
    rationale: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Render a draft-approval card.

    ``thread_id`` is the LangGraph thread id of the paused workflow; it's carried
    in each button's ``value`` so the action handler can resume the right graph.
    """
    why = ""
    if rationale:
        why = "\n".join(f"• {r}" for r in rationale[:3])

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Draft reply* ({domain}) — approve before it goes out:",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{_DRAFT_QUOTE_PREFIX}{proposed_draft}",
            },
        },
    ]
    if why:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Based on: {why}"}],
            }
        )
    blocks.append(
        {
            "type": "actions",
            "block_id": f"adc_approval:{thread_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": ACTION_APPROVE,
                    "value": thread_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Edit"},
                    "action_id": ACTION_EDIT,
                    "value": thread_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": ACTION_REJECT,
                    "value": thread_id,
                },
            ],
        }
    )
    return blocks


def edit_modal_view(
    *, thread_id: str, channel_id: str, proposed_draft: str
) -> dict[str, Any]:
    """Render the edit modal (opened by the approval card's Edit button).

    ``thread_id`` and ``channel_id`` ride in ``private_metadata`` so the
    ``view_submission`` handler can resume the right paused workflow and post
    its confirmation back where the card was. The draft prefills the input so
    the user edits rather than retypes — the resulting diff is the correction
    signal (design 2.2).
    """
    return {
        "type": "modal",
        "callback_id": ACTION_EDIT_SUBMIT,
        "private_metadata": json.dumps(
            {"thread_id": thread_id, "channel": channel_id}
        ),
        "title": {"type": "plain_text", "text": "Edit draft"},
        "submit": {"type": "plain_text", "text": "Save & apply"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": EDIT_MODAL_BLOCK_ID,
                "label": {"type": "plain_text", "text": "Your reply"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": EDIT_MODAL_INPUT_ID,
                    "multiline": True,
                    "initial_value": proposed_draft,
                },
            }
        ],
    }


def extract_draft_from_blocks(blocks: list[dict[str, Any]]) -> str | None:
    """Pull the proposed draft back out of an approval card's blocks.

    The Edit button's click payload carries the original message; the draft
    is the blockquoted section ``approval_blocks`` rendered. Returns ``None``
    if no such section exists (not an approval card)."""
    for block in blocks or []:
        if block.get("type") != "section":
            continue
        text = (block.get("text") or {}).get("text", "")
        if text.startswith(_DRAFT_QUOTE_PREFIX):
            return text[len(_DRAFT_QUOTE_PREFIX):]
    return None


def extract_edit_submission(view: dict[str, Any]) -> tuple[str, str, str] | None:
    """Parse a ``view_submission`` payload's view into
    ``(thread_id, channel_id, edited_text)``, or ``None`` if malformed."""
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
        text = view["state"]["values"][EDIT_MODAL_BLOCK_ID][EDIT_MODAL_INPUT_ID][
            "value"
        ]
    except (KeyError, TypeError, ValueError):
        return None
    thread_id = meta.get("thread_id")
    channel_id = meta.get("channel", "")
    if not thread_id or not text:
        return None
    return thread_id, channel_id, text
