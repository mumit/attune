"""Tests for ingestion/chat_interactions.py — no live services.

Also enforces that the duplicated action-name strings stay in sync with
channels/blocks.py, the same technique test_gchat.py already uses to keep
Slack/Chat in sync.
"""

from __future__ import annotations

from aidedecamp.ingestion.chat_interactions import ChatInteraction, decode_chat_interaction


def _click(fn: str, thread_id: str | None = "t-1") -> dict:
    params = [{"key": "thread_id", "value": thread_id}] if thread_id else []
    return {
        "type": "CARD_CLICKED",
        "action": {"actionMethodName": fn, "parameters": params},
    }


# ---------------------------------------------------------------------------
# decode_chat_interaction
# ---------------------------------------------------------------------------


def test_approve_decodes_to_approved():
    result = decode_chat_interaction(_click("adc_approve", "t-42"))
    assert isinstance(result, ChatInteraction)
    assert result.thread_id == "t-42"
    assert result.decision == "approved"


def test_reject_decodes_to_rejected():
    result = decode_chat_interaction(_click("adc_reject", "t-9"))
    assert result.thread_id == "t-9"
    assert result.decision == "rejected"


def test_edit_returns_none():
    """Edit's initial click never touches the graph — the republisher
    handles it synchronously, so it's deliberately outside this decode."""
    result = decode_chat_interaction(_click("adc_edit", "t-1"))
    assert result is None


def test_unknown_action_returns_none():
    result = decode_chat_interaction(_click("unknown_fn"))
    assert result is None


def test_non_card_clicked_returns_none():
    result = decode_chat_interaction({"type": "MESSAGE"})
    assert result is None


def test_missing_thread_id_returns_none():
    result = decode_chat_interaction(_click("adc_approve", thread_id=None))
    assert result is None


def test_missing_action_returns_none():
    result = decode_chat_interaction({"type": "CARD_CLICKED"})
    assert result is None


# ---------------------------------------------------------------------------
# Edit dialog submit (prompt 02: the third resume-able decision)
# ---------------------------------------------------------------------------


def _edit_submit(text: str | None, thread_id: str = "t-5") -> dict:
    event = _click("adc_edit_submit", thread_id)
    if text is not None:
        event["common"] = {
            "formInputs": {
                "adc_edit_text": {"stringInputs": {"value": [text]}}
            }
        }
    return event


def test_edit_submit_decodes_to_edited_with_text():
    result = decode_chat_interaction(_edit_submit("My rewritten reply."))
    assert isinstance(result, ChatInteraction)
    assert result.thread_id == "t-5"
    assert result.decision == "edited"
    assert result.text == "My rewritten reply."


def test_edit_submit_without_text_returns_none():
    """No edited text -> nothing to resume with; safer to drop than to
    resume 'edited' with an empty draft."""
    assert decode_chat_interaction(_edit_submit(None)) is None
    assert decode_chat_interaction(_edit_submit("")) is None


def test_approve_and_reject_carry_no_text():
    assert decode_chat_interaction(_click("adc_approve", "t-1")).text is None
    assert decode_chat_interaction(_click("adc_reject", "t-1")).text is None


# ---------------------------------------------------------------------------
# Action name parity with channels/blocks.py (duplicated, not imported — see
# module docstring for why)
# ---------------------------------------------------------------------------


def test_action_names_match_blocks_py():
    from aidedecamp.channels.blocks import (
        ACTION_APPROVE,
        ACTION_EDIT_SUBMIT,
        ACTION_REJECT,
    )
    from aidedecamp.ingestion.chat_interactions import (
        _ACTION_APPROVE,
        _ACTION_EDIT_SUBMIT,
        _ACTION_REJECT,
    )

    assert _ACTION_APPROVE == ACTION_APPROVE
    assert _ACTION_REJECT == ACTION_REJECT
    assert _ACTION_EDIT_SUBMIT == ACTION_EDIT_SUBMIT


def test_dialog_field_matches_gchat_cards():
    from aidedecamp.channels.gchat_cards import EDIT_DIALOG_FIELD
    from aidedecamp.ingestion.chat_interactions import _EDIT_DIALOG_FIELD

    assert _EDIT_DIALOG_FIELD == EDIT_DIALOG_FIELD
