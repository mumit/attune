"""The Slack channel (design doc 3.1, 4.6) — a thin door onto the orchestrator.

Design stance: the channel owns no assistant logic. It renders briefs and
approval cards, and translates button clicks into ``Command(resume=...)`` on the
paused LangGraph workflow. Everything that decides, drafts, or learns lives in
the orchestrator; Slack is one of several interchangeable surfaces over the same
brain (the "one brain, many doors" principle).

Transport: Socket Mode (outbound WebSocket). This is a deliberate security
choice — the process holding credentials and memory has no inbound port, which
is the concrete architectural answer to the OpenClaw class of attacks (design
8.1). ngrok / public request URLs are not used.

slack_bolt is a lazy optional import so the package loads without it; the graph,
connector, and client are injected so the wiring is testable without a live
Slack connection.
"""

from __future__ import annotations

from typing import Any, Callable

from .blocks import (
    ACTION_APPROVE,
    ACTION_EDIT,
    ACTION_EDIT_SUBMIT,
    ACTION_REJECT,
    approval_blocks,
    brief_blocks,
    edit_modal_view,
    extract_draft_from_blocks,
    extract_edit_submission,
)


class SlackChannel:
    """Wires a Bolt app to the orchestrator. Construct, then ``start()``.

    Args:
        graph: a compiled draft-and-approve graph (has .invoke).
        resume_fn: callable(thread_id, decision, text) -> resumes the graph.
            Injected so tests don't need a real compiled graph; defaults to a
            Command(resume=...) invoke against ``graph``.
        message_fn: callable(text, user_id, post_text) -> handles an incoming
            DM (design 4.4's conversational Q&A). ``post_text`` is a
            callable(response_text) the handler calls to reply, wrapping
            Bolt's live ``say`` — mirrors the ``post_text`` convention used by
            Google Chat's conversational flow. No default: an unconfigured
            channel should fail loudly on the first DM rather than silently
            ignore users, same rationale as ``GoogleChatChannel``'s
            unconfigured ``send_fn``.
        app: a pre-built Bolt App (tests inject a fake); if None, one is created
            from env tokens on first use.
    """

    def __init__(
        self,
        *,
        graph: Any = None,
        resume_fn: Callable[..., Any] | None = None,
        message_fn: Callable[[str, str, Callable[[str], None]], None] | None = None,
        app: Any = None,
        allowed_users: frozenset[str] | set[str] | None = None,
        on_unauthorized: Callable[[str, str], None] | None = None,
    ):
        self._graph = graph
        self._app = app
        self._resume = resume_fn or self._default_resume
        self._message = message_fn or _no_message_fn
        # Deny-by-default (review finding #1): None/empty refuses every actor.
        # Slack signs the request; only this list authenticates the human.
        self._allowed_users = frozenset(allowed_users or ())
        self._on_unauthorized = on_unauthorized
        if app is not None:
            self._register(app)

    def _authorized(self, actor: str, surface: str) -> bool:
        if actor and actor in self._allowed_users:
            return True
        import logging

        logging.getLogger(__name__).warning(
            "slack: unauthorized actor %s on %s — refused", actor or "<none>", surface
        )
        if self._on_unauthorized is not None:
            self._on_unauthorized(actor, surface)
        return False

    # --- public surface ---------------------------------------------------

    def post_brief(self, say: Callable[..., Any], brief: Any) -> None:
        """Post a morning brief into a channel/thread via a Bolt ``say``."""
        say(
            blocks=brief_blocks(
                summary=brief.summary,
                unread_count=brief.unread_count,
                event_count=brief.event_count,
            ),
            text="Your morning brief",
        )

    def post_approval(self, say: Callable[..., Any], *, thread_id: str, domain: str,
                      proposed_draft: str, rationale: list[str] | None = None,
                      title: str | None = None) -> None:
        """Post a draft-approval card for a paused workflow. ``title``
        overrides the header line so a nudge reads as a nudge, not a
        reply-draft out of nowhere."""
        say(
            blocks=approval_blocks(
                thread_id=thread_id,
                domain=domain,
                proposed_draft=proposed_draft,
                rationale=rationale,
                title=title,
            ),
            text="A draft needs your approval",
        )

    def start(self) -> None:  # pragma: no cover - requires live Slack
        """Run the Socket Mode handler (blocks)."""
        app = self._ensure_app()
        from slack_bolt.adapter.socket_mode import SocketModeHandler
        import os

        SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()

    # --- internals --------------------------------------------------------

    def _default_resume(
        self, thread_id: str, decision: str, text: str | None, *, actor: str | None = None
    ):
        from ..orchestrator import resume_workflow

        return resume_workflow(self._graph, thread_id, decision, text)

    def _ensure_app(self):  # pragma: no cover - requires slack_bolt + env
        if self._app is None:
            import os
            from slack_bolt import App

            self._app = App(token=os.environ["SLACK_BOT_TOKEN"])
            self._register(self._app)
        return self._app

    def _register(self, app: Any) -> None:
        """Attach the three approval-button handlers to a Bolt app.

        Each handler acks immediately (Slack's 3s rule), extracts the workflow
        thread_id from the button value, and resumes the graph with the matching
        decision. Edit opens a modal in the real app; here it resumes with an
        'edited' decision whose text the modal supplies."""

        def _actor(body):  # noqa: ANN001
            return (body.get("user") or {}).get("id", "")

        @app.action(ACTION_APPROVE)
        def _approve(ack, body, respond):  # noqa: ANN001
            ack()
            actor = _actor(body)
            if not self._authorized(actor, "approve"):
                respond(text=_REFUSAL.format(actor=actor), replace_original=False)
                return
            thread_id = body["actions"][0]["value"]
            result = self._resume(thread_id, "approved", None, actor=actor)
            from ..orchestrator import apply_confirmation

            respond(
                text=apply_confirmation("approved", result), replace_original=True
            )

        @app.action(ACTION_REJECT)
        def _reject(ack, body, respond):  # noqa: ANN001
            ack()
            actor = _actor(body)
            if not self._authorized(actor, "reject"):
                respond(text=_REFUSAL.format(actor=actor), replace_original=False)
                return
            thread_id = body["actions"][0]["value"]
            result = self._resume(thread_id, "rejected", None, actor=actor)
            from ..orchestrator import apply_confirmation

            respond(
                text=apply_confirmation("rejected", result), replace_original=True
            )

        @app.action(ACTION_EDIT)
        def _edit(ack, body, client):  # noqa: ANN001
            ack()
            actor = _actor(body)
            if not self._authorized(actor, "edit"):
                return
            thread_id = body["actions"][0]["value"]
            draft = extract_draft_from_blocks(
                (body.get("message") or {}).get("blocks") or []
            )
            channel_id = (body.get("channel") or {}).get("id", "")
            client.views_open(
                trigger_id=body["trigger_id"],
                view=edit_modal_view(
                    thread_id=thread_id,
                    channel_id=channel_id,
                    proposed_draft=draft or "",
                ),
            )

        @app.view(ACTION_EDIT_SUBMIT)
        def _edit_submit(ack, body, client):  # noqa: ANN001
            ack()
            actor = _actor(body)
            if not self._authorized(actor, "edit-submit"):
                return
            parsed = extract_edit_submission(body.get("view") or {})
            if parsed is None:
                return
            thread_id, channel_id, text = parsed
            result = self._resume(thread_id, "edited", text, actor=actor)
            from ..orchestrator import apply_confirmation

            if channel_id:
                client.chat_postMessage(
                    channel=channel_id, text=apply_confirmation("edited", result)
                )

        @app.event("message")
        def _message(event, say):  # noqa: ANN001
            # Design 4.4's message.im: DMs only, and never our own messages
            # (bot_id/subtype guard against self-reply loops, same rationale
            # as chat_events.process_chat_event's BOT-sender filter).
            if event.get("channel_type") != "im":
                return
            if event.get("bot_id") or event.get("subtype") == "bot_message":
                return

            text = event.get("text", "")
            user = event.get("user", "")
            if not self._authorized(user, "dm"):
                say(text=_REFUSAL.format(actor=user))
                return

            def _post_text(response: str) -> None:
                say(text=response)

            self._message(text, user, _post_text)


_REFUSAL = (
    "⛔ I don't recognize you (your Slack id is {actor}). This assistant "
    "acts for one person; ask the owner to add your id to "
    "ADC_SLACK_ALLOWED_USERS if this is a mistake."
)


def _no_message_fn(text: str, user_id: str, post_text: Callable[[str], Any]) -> None:
    raise RuntimeError(
        "SlackChannel: no message_fn provided. Pass message_fn=<a callable "
        "bound to dispatcher.handle_slack_message> or inject a fake for tests."
    )


def make_slack_say(bot_token: str, channel: str) -> Callable[..., Any]:
    """Build a ``say``-shaped callable for proactive posts — a morning brief or
    a Gmail-triggered approval card, neither of which arrives inside a live
    Slack event with its own ``say`` in scope (unlike button-click handlers,
    which get one from Bolt). Lazily imports ``slack_sdk`` (bundled with
    slack_bolt) so the module loads without it.

    Use with :class:`SlackChannel`'s ``post_brief``/``post_approval``::

        say = make_slack_say(settings.slack_bot_token, settings.slack_default_channel)
        slack_channel.post_approval(say, thread_id=..., domain=..., proposed_draft=...)
    """

    def say(**kwargs: Any) -> Any:
        from slack_sdk import WebClient

        client = WebClient(token=bot_token)
        return client.chat_postMessage(channel=channel, **kwargs)

    return say
