"""The always-on entrypoint (design doc 4.6) — wires every already-built
collaborator into one running process.

Everything this module imports already exists and is independently tested:
``app.py`` (graph + memory + client + audit log), ``credentials.py`` (Google
auth), ``connectors`` (Workspace access), ``ingestion`` (Gmail/Chat event
reconciliation), ``channels`` (Slack + Google Chat), ``dispatcher.py`` (the
routing seam). What this module adds is the *wiring*: binding dispatcher's
channel-agnostic callables to real channels, and pumping decoded events from a
pull subscription into the dispatcher.

Two different kinds of code live here, deliberately kept apart:

- **Wiring logic** (``build_runtime``, ``process_gmail_notification``,
  ``process_chat_event``, ``renew_*``): fully testable offline with injected
  fakes, same as every other module in this codebase.
- **Live loops** (``run``, ``run_gmail_pubsub_loop``, ``run_chat_pubsub_loop``):
  thin, ``pragma: no cover``, matching the existing precedent set by
  ``SlackChannel.start()`` — they need a live GCP project and Slack workspace
  to exercise, so correctness here rests on the wiring logic they call being
  independently tested, not on testing the loop itself.

Per rule 5 (no inbound port on the credential-holding process): Gmail and Chat
notifications arrive via a synchronous **pull** subscription (outbound-only —
this process calls out to Pub/Sub, nothing calls in), and Slack via Socket
Mode. No listener socket is ever opened here.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Callable

from .app import AppContext, build_app
from .brief import assemble_brief
from .config import Settings
from .connectors import WorkspaceConnector, make_connector
from .credentials import load_google_credentials
from .dispatcher import handle_chat_message, handle_gmail_notification, handle_slack_message
from .ingestion import (
    HistoryExpired,
    JsonChatSubscriptionState,
    JsonGmailWatchState,
    ensure_subscription,
    ensure_watch,
)
from .ingestion.chat_events import SubscriptionState
from .ingestion.gmail_watch import WatchState


@dataclass
class Runtime:
    """The assembled always-on process. Construct via :func:`build_runtime`."""

    app: AppContext
    settings: Settings
    connector: WorkspaceConnector
    gmail_service: Any
    watch_state: WatchState
    chat_state: SubscriptionState
    slack: Any = None          # channels.SlackChannel, or None if not configured
    slack_say: Callable[..., Any] | None = None
    gchat: Any = None          # channels.GoogleChatChannel, or None if not configured
    chat_events_service: Any = None  # raw workspaceevents API resource, for renewal

    # --- event processing (testable) ---------------------------------------

    def process_gmail_notification(self, notification: dict[str, Any]) -> list[str]:
        """Reconcile one decoded Gmail Pub/Sub notification and post any
        resulting approval cards to every configured channel."""

        def _post_approval(
            thread_id: str, draft: str, rationale: list[str] | None
        ) -> None:
            if self.slack is not None and self.slack_say is not None:
                self.slack.post_approval(
                    self.slack_say,
                    thread_id=thread_id,
                    domain="mail",
                    proposed_draft=draft,
                    rationale=rationale,
                )
            if self.gchat is not None and self.settings.chat_default_space:
                self.gchat.post_approval(
                    self.settings.chat_default_space,
                    thread_id=thread_id,
                    domain="mail",
                    proposed_draft=draft,
                    rationale=rationale,
                )

        return handle_gmail_notification(
            self.app,
            notification,
            gmail_service=self.gmail_service,
            watch_state=self.watch_state,
            connector=self.connector,
            post_approval=_post_approval,
            user_id=self.settings.user_id,
            audit_log=self.app.audit_log,
        )

    def process_chat_event(self, event: dict[str, Any]) -> None:
        """Decode one Chat space event and post the reply back to that space."""
        if self.gchat is None:
            return

        def _post_text(text: str) -> None:
            self.gchat.post_text(self.settings.chat_default_space, text)

        def _brief_fn() -> str:
            brief = assemble_brief(self.connector, self.app.client)
            return brief.summary

        handle_chat_message(
            self.app,
            event,
            post_text=_post_text,
            user_id=self.settings.user_id,
            brief_fn=_brief_fn,
        )

    # --- watch/subscription renewal (testable, called on a daily schedule) --

    def renew_gmail_watch(self, *, force: bool = False):
        return ensure_watch(
            self.gmail_service,
            self.watch_state,
            email=self.settings.user_id,
            topic=self.settings.gmail_pubsub_topic,
            force=force,
        )

    def renew_chat_subscription(self, *, force: bool = False):
        return ensure_subscription(
            self.chat_events_service,
            self.chat_state,
            space=self.settings.chat_default_space,
            topic=self.settings.chat_pubsub_topic,
            force=force,
        )

    # --- live loops (pragma: no cover — need real GCP/Slack) ----------------

    def run_gmail_pubsub_loop(self) -> None:  # pragma: no cover
        """Pull Gmail Pub/Sub notifications forever and dispatch each one."""
        from google.cloud import pubsub_v1

        subscriber = pubsub_v1.SubscriberClient()
        subscription = self.settings.gmail_pubsub_subscription
        while True:
            response = subscriber.pull(
                request={"subscription": subscription, "max_messages": 10},
                timeout=30,
            )
            for received in response.received_messages:
                notification = json.loads(received.message.data)
                try:
                    self.process_gmail_notification(notification)
                except HistoryExpired:
                    self.renew_gmail_watch(force=True)
                subscriber.acknowledge(
                    request={
                        "subscription": subscription,
                        "ack_ids": [received.ack_id],
                    }
                )

    def run_chat_pubsub_loop(self) -> None:  # pragma: no cover
        """Pull Chat Workspace Events notifications forever and dispatch each."""
        from google.cloud import pubsub_v1

        subscriber = pubsub_v1.SubscriberClient()
        subscription = self.settings.chat_pubsub_subscription
        while True:
            response = subscriber.pull(
                request={"subscription": subscription, "max_messages": 10},
                timeout=30,
            )
            for received in response.received_messages:
                event = json.loads(received.message.data)
                self.process_chat_event(event)
                subscriber.acknowledge(
                    request={
                        "subscription": subscription,
                        "ack_ids": [received.ack_id],
                    }
                )

    def run(self) -> None:  # pragma: no cover
        """Start the always-on process: Gmail/Chat pull loops run in background
        daemon threads; Slack Socket Mode blocks the main thread (or, absent
        Slack, the main thread just waits so the daemon threads keep running)."""
        if self.settings.gmail_pubsub_subscription:
            threading.Thread(target=self.run_gmail_pubsub_loop, daemon=True).start()
        if self.settings.chat_pubsub_subscription and self.gchat is not None:
            threading.Thread(target=self.run_chat_pubsub_loop, daemon=True).start()

        if self.slack is not None:
            self.slack.start()
        else:
            threading.Event().wait()


def build_runtime(
    settings: Settings | None = None,
    *,
    app: AppContext | None = None,
    connector: WorkspaceConnector | None = None,
    credentials: Any = None,
    gmail_service: Any = None,
    watch_state: WatchState | None = None,
    chat_state: SubscriptionState | None = None,
    slack: Any = None,
    slack_say: Callable[..., Any] | None = None,
    gchat: Any = None,
    chat_events_service: Any = None,
) -> Runtime:
    """Assemble a :class:`Runtime` from config and optional overrides.

    Mirrors ``app.build_app``'s override-or-build-real pattern: pass fakes for
    every collaborator in tests; in production, omit overrides and each is
    constructed from ``settings``.

    - *credentials* — via ``load_google_credentials(settings)``
    - *connector*   — via ``make_connector(settings, credentials=...)``
    - *gmail_service* — a raw Gmail API resource, built from *credentials*
      (ingestion needs this directly; it's independent of *connector*, which
      may be the MCP implementation instead)
    - *watch_state* / *chat_state* — ``JsonGmailWatchState`` /
      ``JsonChatSubscriptionState``, backed by ``settings.*_state_path``
    - *slack* / *gchat* — only built when the relevant tokens/state are
      present in ``settings``; a deployment need not run both channels
    """
    settings = settings or Settings.from_env()
    resolved_app = app or build_app(settings)

    resolved_credentials = credentials
    if resolved_credentials is None and (connector is None or gmail_service is None):
        resolved_credentials = load_google_credentials(settings)

    resolved_connector = connector or make_connector(
        settings, credentials=resolved_credentials
    )

    resolved_gmail_service = gmail_service
    if resolved_gmail_service is None:  # pragma: no cover - requires live creds
        from googleapiclient.discovery import build as _build

        resolved_gmail_service = _build(
            "gmail", "v1", credentials=resolved_credentials
        )

    resolved_watch_state = watch_state or JsonGmailWatchState(
        settings.gmail_watch_state_path
    )
    resolved_chat_state = chat_state or JsonChatSubscriptionState(
        settings.chat_subscription_state_path
    )

    resolved_slack = slack
    resolved_slack_say = slack_say
    if resolved_slack is None and settings.slack_bot_token:
        from .channels import SlackChannel, make_slack_say

        def _slack_message_fn(text, user_id, post_text):  # noqa: ANN001
            handle_slack_message(
                resolved_app,
                text=text,
                user_id=user_id,
                post_text=post_text,
                brief_fn=lambda: assemble_brief(
                    resolved_connector, resolved_app.client
                ).summary,
            )

        resolved_slack = SlackChannel(
            graph=resolved_app.graph, message_fn=_slack_message_fn
        )
        if settings.slack_default_channel:
            resolved_slack_say = make_slack_say(
                settings.slack_bot_token, settings.slack_default_channel
            )

    resolved_gchat = gchat
    if resolved_gchat is None and settings.chat_default_space:
        from .channels import GoogleChatChannel, make_chat_send_fn

        resolved_gchat = GoogleChatChannel(
            graph=resolved_app.graph,
            send_fn=make_chat_send_fn(resolved_credentials),
        )

    resolved_chat_events_service = chat_events_service
    if resolved_chat_events_service is None:  # pragma: no cover - requires live creds
        from googleapiclient.discovery import build as _build

        resolved_chat_events_service = _build(
            "workspaceevents", "v1", credentials=resolved_credentials
        )

    return Runtime(
        app=resolved_app,
        settings=settings,
        connector=resolved_connector,
        gmail_service=resolved_gmail_service,
        watch_state=resolved_watch_state,
        chat_state=resolved_chat_state,
        slack=resolved_slack,
        slack_say=resolved_slack_say,
        gchat=resolved_gchat,
        chat_events_service=resolved_chat_events_service,
    )
