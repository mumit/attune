"""Tests for the Google Chat channel.

No live Chat workspace, no google-api-python-client needed. send_fn and
resume_fn are injected fakes; handle_interaction receives canned event dicts
the republisher would forward.
"""

from __future__ import annotations

import pytest

from attune.channels import (
    ACTION_APPROVE,
    ACTION_EDIT,
    ACTION_REJECT,
    GoogleChatChannel,
    approval_card,
    brief_card,
)


# ---------------------------------------------------------------------------
# brief_card builders
# ---------------------------------------------------------------------------


def test_brief_card_top_level_shape():
    payload = brief_card(summary="all quiet", unread_count=3, event_count=2)
    assert "cardsV2" in payload
    assert len(payload["cardsV2"]) == 1


def test_brief_card_header_contains_counts():
    payload = brief_card(summary="all quiet", unread_count=3, event_count=2)
    subtitle = payload["cardsV2"][0]["card"]["header"]["subtitle"]
    assert "3" in subtitle
    assert "2" in subtitle


def test_brief_card_body_contains_summary():
    payload = brief_card(summary="Two urgent items.", unread_count=1, event_count=0)
    section = payload["cardsV2"][0]["card"]["sections"][0]
    texts = [w["textParagraph"]["text"] for w in section["widgets"] if "textParagraph" in w]
    assert any("Two urgent items." in t for t in texts)


# ---------------------------------------------------------------------------
# approval_card builders
# ---------------------------------------------------------------------------


def test_approval_card_carries_thread_id_in_all_buttons():
    payload = approval_card(
        thread_id="t-42", domain="mail", proposed_draft="Hi there"
    )
    buttons = _get_buttons(payload)
    assert len(buttons) == 3
    for btn in buttons:
        params = btn["onClick"]["action"]["parameters"]
        assert any(p["key"] == "thread_id" and p["value"] == "t-42" for p in params)


def test_approval_card_action_function_names():
    payload = approval_card(
        thread_id="t-1", domain="mail", proposed_draft="draft"
    )
    fns = {btn["onClick"]["action"]["function"] for btn in _get_buttons(payload)}
    assert fns == {ACTION_APPROVE, ACTION_EDIT, ACTION_REJECT}


def test_approval_card_contains_draft_text():
    payload = approval_card(
        thread_id="t-1", domain="mail", proposed_draft="Please confirm by Friday."
    )
    section = payload["cardsV2"][0]["card"]["sections"][0]
    texts = [w["textParagraph"]["text"] for w in section["widgets"] if "textParagraph" in w]
    assert any("Please confirm by Friday." in t for t in texts)


def test_approval_card_rationale_shown():
    payload = approval_card(
        thread_id="t-1",
        domain="mail",
        proposed_draft="x",
        rationale=["prefers short replies", "never use jargon"],
    )
    section = payload["cardsV2"][0]["card"]["sections"][0]
    texts = [w["textParagraph"]["text"] for w in section["widgets"] if "textParagraph" in w]
    assert any("prefers short replies" in t for t in texts)


def test_approval_card_rationale_capped_at_three():
    payload = approval_card(
        thread_id="t-1",
        domain="mail",
        proposed_draft="x",
        rationale=["a", "b", "c", "d", "e"],
    )
    section = payload["cardsV2"][0]["card"]["sections"][0]
    rationale_texts = [
        w["textParagraph"]["text"]
        for w in section["widgets"]
        if "textParagraph" in w and "Based on:" in w["textParagraph"]["text"]
    ]
    assert rationale_texts  # rationale widget present
    # items beyond the third ("d", "e") must not appear as bullet points
    assert "• d" not in rationale_texts[0] and "• e" not in rationale_texts[0]


def test_approval_card_no_rationale_widget_when_absent():
    payload = approval_card(thread_id="t-1", domain="mail", proposed_draft="x")
    section = payload["cardsV2"][0]["card"]["sections"][0]
    based_on = [
        w for w in section["widgets"]
        if "textParagraph" in w and "Based on:" in w["textParagraph"]["text"]
    ]
    assert based_on == []


# ---------------------------------------------------------------------------
# handle_interaction — button click routing
# ---------------------------------------------------------------------------


def _channel(**kwargs):
    resumes = []
    ch = GoogleChatChannel(
        resume_fn=lambda tid, decision, text: resumes.append((tid, decision, text)),
        **kwargs,
    )
    return ch, resumes


def _click(fn: str, thread_id: str) -> dict:
    return {
        "type": "CARD_CLICKED",
        "action": {
            "actionMethodName": fn,
            "parameters": [{"key": "thread_id", "value": thread_id}],
        },
    }


def test_handle_approve_resumes_graph():
    ch, resumes = _channel()
    response = ch.handle_interaction(_click(ACTION_APPROVE, "t-7"))
    assert resumes == [("t-7", "approved", None)]
    assert "Approved" in response["text"]


def test_handle_reject_resumes_graph():
    ch, resumes = _channel()
    response = ch.handle_interaction(_click(ACTION_REJECT, "t-9"))
    assert resumes == [("t-9", "rejected", None)]
    assert "Rejected" in response["text"]


def test_handle_unknown_action_returns_none():
    ch, _ = _channel()
    result = ch.handle_interaction(_click("unknown_fn", "t-1"))
    assert result is None


# ---------------------------------------------------------------------------
# Edit flow (prompt 02): dialog-open prefill + dialog-submit resume
# ---------------------------------------------------------------------------


def test_edit_click_returns_prefilled_dialog():
    from attune.channels import ACTION_EDIT_SUBMIT, EDIT_DIALOG_FIELD

    ch, resumes = _channel()
    event = _click(ACTION_EDIT, "t-7")
    # Chat echoes the clicked card in the event; the dialog prefills from it.
    event["message"] = approval_card(
        thread_id="t-7", domain="mail", proposed_draft="Original draft."
    )
    response = ch.handle_interaction(event)

    assert resumes == []  # dialog-open never touches the graph
    dialog = response["actionResponse"]["dialogAction"]["dialog"]
    widgets = dialog["body"]["sections"][0]["widgets"]
    text_input = widgets[0]["textInput"]
    assert text_input["name"] == EDIT_DIALOG_FIELD
    assert text_input["value"] == "Original draft."
    submit = widgets[1]["buttonList"]["buttons"][0]["onClick"]["action"]
    assert submit["function"] == ACTION_EDIT_SUBMIT
    assert submit["parameters"] == [{"key": "thread_id", "value": "t-7"}]


def test_edit_click_without_card_prefills_empty():
    ch, _ = _channel()
    response = ch.handle_interaction(_click(ACTION_EDIT, "t-7"))
    dialog = response["actionResponse"]["dialogAction"]["dialog"]
    assert dialog["body"]["sections"][0]["widgets"][0]["textInput"]["value"] == ""


def test_edit_submit_resumes_edited_with_text():
    from attune.channels import ACTION_EDIT_SUBMIT

    ch, resumes = _channel()
    event = _click(ACTION_EDIT_SUBMIT, "t-7")
    event["common"] = {
        "formInputs": {
            "attune_edit_text": {"stringInputs": {"value": ["My rewrite."]}}
        }
    }
    response = ch.handle_interaction(event)

    assert resumes == [("t-7", "edited", "My rewrite.")]
    assert "Edited" in response["text"]


def test_handle_non_card_clicked_returns_none():
    ch, _ = _channel()
    result = ch.handle_interaction({"type": "MESSAGE", "text": "hello"})
    assert result is None


def test_handle_missing_thread_id_returns_none():
    ch, resumes = _channel()
    event = {
        "type": "CARD_CLICKED",
        "action": {
            "actionMethodName": ACTION_APPROVE,
            "parameters": [],  # no thread_id
        },
    }
    result = ch.handle_interaction(event)
    assert result is None
    assert resumes == []


# ---------------------------------------------------------------------------
# post_brief / post_approval — send_fn wiring
# ---------------------------------------------------------------------------


def _send_recorder():
    calls = []

    def send_fn(space, payload):
        calls.append((space, payload))

    return send_fn, calls


def test_post_brief_calls_send_fn():
    send_fn, calls = _send_recorder()
    ch = GoogleChatChannel(send_fn=send_fn)

    class _Brief:
        summary = "2 unread"
        unread_count = 2
        event_count = 1

    ch.post_brief("spaces/ABC", _Brief())
    assert len(calls) == 1
    space, payload = calls[0]
    assert space == "spaces/ABC"
    assert "cardsV2" in payload


def test_post_approval_calls_send_fn():
    send_fn, calls = _send_recorder()
    ch = GoogleChatChannel(send_fn=send_fn)
    ch.post_approval(
        "spaces/ABC",
        thread_id="t-1",
        domain="mail",
        proposed_draft="Hey there",
    )
    assert len(calls) == 1
    _, payload = calls[0]
    assert "cardsV2" in payload
    buttons = _get_buttons(payload)
    assert any(
        p["value"] == "t-1"
        for btn in buttons
        for p in btn["onClick"]["action"]["parameters"]
    )


def test_post_text_calls_send_fn_with_plain_text():
    send_fn, calls = _send_recorder()
    ch = GoogleChatChannel(send_fn=send_fn)
    ch.post_text("spaces/ABC", "The answer is 42.")
    assert calls == [("spaces/ABC", {"text": "The answer is 42."})]


def test_no_send_fn_raises_on_post():
    ch = GoogleChatChannel()  # no send_fn
    with pytest.raises(RuntimeError, match="send_fn"):

        class _Brief:
            summary = ""
            unread_count = 0
            event_count = 0

        ch.post_brief("spaces/ABC", _Brief())


# ---------------------------------------------------------------------------
# action name parity with Slack (same strings, different semantic context)
# ---------------------------------------------------------------------------


def test_action_names_match_slack_blocks():
    from attune.channels.blocks import (
        ACTION_APPROVE as SLACK_APPROVE,
        ACTION_EDIT as SLACK_EDIT,
        ACTION_REJECT as SLACK_REJECT,
    )
    assert ACTION_APPROVE == SLACK_APPROVE
    assert ACTION_EDIT == SLACK_EDIT
    assert ACTION_REJECT == SLACK_REJECT


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _get_buttons(payload: dict) -> list[dict]:
    """Extract buttons from the first section of a cardsV2 payload."""
    section = payload["cardsV2"][0]["card"]["sections"][0]
    for widget in section["widgets"]:
        if "buttonList" in widget:
            return widget["buttonList"]["buttons"]
    return []
