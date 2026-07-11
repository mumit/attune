"""Slack channel tests. A fake Bolt app captures registered handlers so we can
fire button actions and assert they resume the graph — no live Slack, no
slack_bolt needed.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import patch

from aidedecamp.channels import (
    ACTION_APPROVE,
    ACTION_EDIT,
    ACTION_REJECT,
    SlackChannel,
    approval_blocks,
    brief_blocks,
    make_slack_say,
)


class FakeApp:
    """Captures @app.action and @app.event handlers by id/name."""

    def __init__(self):
        self.handlers = {}
        self.event_handlers = {}

    def action(self, action_id):
        def deco(fn):
            self.handlers[action_id] = fn
            return fn
        return deco

    def event(self, event_name):
        def deco(fn):
            self.event_handlers[event_name] = fn
            return fn
        return deco


def _say_recorder():
    calls = []

    def say(**kwargs):
        calls.append(kwargs)

    return say, calls


# --- block builders ------------------------------------------------------

def test_brief_blocks_shape():
    blocks = brief_blocks(summary="all quiet", unread_count=3, event_count=2)
    assert blocks[0]["type"] == "header"
    assert "3" in blocks[1]["elements"][0]["text"]
    assert "all quiet" in blocks[-1]["text"]["text"]


def test_approval_blocks_carry_thread_id():
    blocks = approval_blocks(
        thread_id="t-42", domain="mail", proposed_draft="Hi there",
        rationale=["prefers short replies"],
    )
    actions = [b for b in blocks if b["type"] == "actions"][0]
    # every button carries the workflow thread_id so the click can resume it
    assert all(el["value"] == "t-42" for el in actions["elements"])
    ids = {el["action_id"] for el in actions["elements"]}
    assert ids == {ACTION_APPROVE, ACTION_EDIT, ACTION_REJECT}


# --- button -> resume routing -------------------------------------------

def _make_channel():
    resumes = []
    ch = SlackChannel(resume_fn=lambda tid, decision, text: resumes.append(
        (tid, decision, text)), app=FakeApp())
    return ch, resumes


def test_approve_button_resumes_graph_approved():
    ch, resumes = _make_channel()
    ack_called = []
    body = {"actions": [{"value": "t-7"}]}
    ch._app.handlers[ACTION_APPROVE](
        ack=lambda: ack_called.append(True), body=body, respond=lambda **k: None
    )
    assert ack_called == [True]           # acked within Slack's 3s window
    assert resumes == [("t-7", "approved", None)]


def test_reject_button_resumes_graph_rejected():
    ch, resumes = _make_channel()
    body = {"actions": [{"value": "t-9"}]}
    ch._app.handlers[ACTION_REJECT](
        ack=lambda: None, body=body, respond=lambda **k: None
    )
    assert resumes == [("t-9", "rejected", None)]


# --- message -> conversational reply routing -----------------------------


def _im_event(text="hello", user="U1", **overrides):
    event = {"channel_type": "im", "text": text, "user": user}
    event.update(overrides)
    return event


def test_message_handler_calls_message_fn_for_dm():
    calls = []
    app = FakeApp()
    ch = SlackChannel(
        app=app,
        message_fn=lambda text, user, post_text: calls.append((text, user, post_text)),
    )
    say, say_calls = _say_recorder()

    app.event_handlers["message"](event=_im_event("what's on my plate?"), say=say)

    assert len(calls) == 1
    text, user, post_text = calls[0]
    assert text == "what's on my plate?"
    assert user == "U1"

    post_text("here's your answer")
    assert say_calls == [{"text": "here's your answer"}]


def test_message_handler_ignores_non_im_messages():
    calls = []
    app = FakeApp()
    ch = SlackChannel(
        app=app,
        message_fn=lambda text, user, post_text: calls.append((text, user)),
    )
    say, _ = _say_recorder()

    app.event_handlers["message"](
        event=_im_event(channel_type="channel"), say=say
    )

    assert calls == []


def test_message_handler_ignores_bot_id_messages():
    calls = []
    app = FakeApp()
    ch = SlackChannel(
        app=app,
        message_fn=lambda text, user, post_text: calls.append((text, user)),
    )
    say, _ = _say_recorder()

    app.event_handlers["message"](event=_im_event(bot_id="B123"), say=say)

    assert calls == []


def test_message_handler_ignores_bot_message_subtype():
    calls = []
    app = FakeApp()
    ch = SlackChannel(
        app=app,
        message_fn=lambda text, user, post_text: calls.append((text, user)),
    )
    say, _ = _say_recorder()

    app.event_handlers["message"](
        event=_im_event(subtype="bot_message"), say=say
    )

    assert calls == []


def test_unconfigured_message_fn_raises_on_dm():
    app = FakeApp()
    ch = SlackChannel(app=app)  # no message_fn injected
    say, _ = _say_recorder()

    import pytest
    with pytest.raises(RuntimeError, match="message_fn"):
        app.event_handlers["message"](event=_im_event("hi"), say=say)


def test_post_brief_uses_say():
    ch = SlackChannel(app=FakeApp())
    say, calls = _say_recorder()

    class B:
        summary = "2 unread"
        unread_count = 2
        event_count = 0

    ch.post_brief(say, B())
    assert calls and calls[0]["blocks"][0]["type"] == "header"


def test_post_approval_uses_say():
    ch = SlackChannel(app=FakeApp())
    say, calls = _say_recorder()
    ch.post_approval(say, thread_id="t1", domain="mail", proposed_draft="hey")
    assert calls
    actions = [b for b in calls[0]["blocks"] if b["type"] == "actions"][0]
    assert actions["elements"][0]["value"] == "t1"


# --- make_slack_say (proactive posting, no live event context) -----------


def _fake_slack_sdk_module():
    calls = []

    class _FakeWebClient:
        def __init__(self, token):
            calls.append({"token": token})

        def chat_postMessage(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True}

    mod = ModuleType("slack_sdk")
    mod.WebClient = _FakeWebClient
    return mod, calls


def test_make_slack_say_builds_client_with_bot_token():
    mod, calls = _fake_slack_sdk_module()
    with patch.dict(sys.modules, {"slack_sdk": mod}):
        say = make_slack_say("xoxb-token", "C123")
        say(text="hello")

    assert calls[0] == {"token": "xoxb-token"}


def test_make_slack_say_posts_to_configured_channel():
    mod, calls = _fake_slack_sdk_module()
    with patch.dict(sys.modules, {"slack_sdk": mod}):
        say = make_slack_say("xoxb-token", "C123")
        say(text="hello", blocks=[{"type": "header"}])

    post_call = calls[1]
    assert post_call["channel"] == "C123"
    assert post_call["text"] == "hello"
    assert post_call["blocks"] == [{"type": "header"}]


def test_make_slack_say_returns_result():
    mod, _ = _fake_slack_sdk_module()
    with patch.dict(sys.modules, {"slack_sdk": mod}):
        say = make_slack_say("xoxb-token", "C123")
        result = say(text="hi")

    assert result == {"ok": True}


def test_make_slack_say_usable_with_post_approval():
    """make_slack_say's output is a drop-in `say` for SlackChannel.post_approval."""
    mod, calls = _fake_slack_sdk_module()
    ch = SlackChannel(app=FakeApp())
    with patch.dict(sys.modules, {"slack_sdk": mod}):
        say = make_slack_say("xoxb-token", "C123")
        ch.post_approval(say, thread_id="t1", domain="mail", proposed_draft="hey")

    post_call = calls[1]
    assert post_call["channel"] == "C123"
    actions = [b for b in post_call["blocks"] if b["type"] == "actions"][0]
    assert actions["elements"][0]["value"] == "t1"
