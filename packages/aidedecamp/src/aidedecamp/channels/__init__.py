"""Interaction surfaces (design doc 3.1). One brain, many doors.

Slack (Socket Mode, approvals via buttons) and Google Chat (Cards v2,
CARD_CLICKED interaction events via thin republisher). Browser and voice later.
All are thin surfaces over the single orchestrator and memory store — they
render and collect, they do not decide.
"""

from .blocks import (
    ACTION_APPROVE,
    ACTION_EDIT,
    ACTION_EDIT_SUBMIT,
    ACTION_REJECT,
    approval_blocks,
    brief_blocks,
    edit_modal_view,
    extract_draft_from_blocks,
)
from .slack import SlackChannel, make_slack_say
from .gchat import GoogleChatChannel, make_chat_send_fn
from .gchat_cards import (
    EDIT_DIALOG_FIELD,
    approval_card,
    brief_card,
    edit_dialog,
    extract_draft_from_card_event,
)

__all__ = [
    "SlackChannel",
    "make_slack_say",
    "GoogleChatChannel",
    "make_chat_send_fn",
    "brief_blocks",
    "approval_blocks",
    "edit_modal_view",
    "extract_draft_from_blocks",
    "brief_card",
    "approval_card",
    "edit_dialog",
    "extract_draft_from_card_event",
    "ACTION_APPROVE",
    "ACTION_EDIT",
    "ACTION_EDIT_SUBMIT",
    "ACTION_REJECT",
    "EDIT_DIALOG_FIELD",
]
