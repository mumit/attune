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
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .app import AppContext, build_app
from .brief import assemble_brief
from .config import Settings
from .conversation import JsonConversationLog
from .connectors import WorkspaceConnector, make_connector
from .credentials import load_google_credentials
from .dispatcher import (
    handle_calendar_notification,
    handle_chat_interaction,
    handle_chat_message,
    handle_gmail_notification,
    handle_slack_message,
)
from .ingestion import (
    HistoryExpired,
    JsonCalendarChannelState,
    JsonCalendarSyncState,
    JsonChatSubscriptionState,
    JsonGmailWatchState,
    ensure_calendar_watch,
    ensure_subscription,
    ensure_watch,
)
from .orchestrator import (
    JsonPendingApprovals,
    make_connector_apply_fn,
    resume_workflow,
    sweep_ignored,
)
from .ingestion.calendar_sync import SyncState
from .ingestion.calendar_watch import ChannelState
from .ingestion.chat_events import SubscriptionState
from .ingestion.gmail_watch import WatchState

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 300
BACKOFF_INITIAL_SECONDS = 1
BACKOFF_MAX_SECONDS = 60


def _assemble_runtime_brief(connector: Any, app: AppContext, settings: Settings):
    """The one place brief-assembly arguments are derived from settings, used
    by every surface that produces a brief (scheduled post, Slack DM, Chat
    message). ``user_email`` only when user_id is a real address — the quiet-
    thread section needs something to match the last sender against, and the
    Gmail "me" alias matches nothing."""
    user = settings.user_id
    return assemble_brief(
        connector,
        app.client,
        store=app.store,
        user_id=user,
        user_email=user if "@" in user else None,
        tz=settings.timezone,
    )


def next_backoff(current: float) -> float:
    """Exponential backoff for pull-transport failures: doubles, capped."""
    return min(current * 2, BACKOFF_MAX_SECONDS)


class LoopStats:
    """Per-loop counters + periodic heartbeat line, so "is it alive?" is one
    ``journalctl | grep heartbeat`` away (roadmap prompt 06)."""

    def __init__(self, name: str, interval_seconds: int = HEARTBEAT_SECONDS):
        self.name = name
        self._interval = interval_seconds
        self.pulled = 0
        self.handled = 0
        self.failed = 0
        self._last_beat: datetime | None = None

    def record(self, ok: bool) -> None:
        self.pulled += 1
        if ok:
            self.handled += 1
        else:
            self.failed += 1

    def maybe_beat(self, now: datetime | None = None) -> str | None:
        """A heartbeat line when the interval has elapsed (counters reset),
        else None. The first call arms the timer without beating."""
        now = now or datetime.now(timezone.utc)
        if self._last_beat is None:
            self._last_beat = now
            return None
        if (now - self._last_beat).total_seconds() < self._interval:
            return None
        line = (
            f"heartbeat {self.name}: pulled={self.pulled} "
            f"handled={self.handled} failed={self.failed}"
        )
        self.pulled = self.handled = self.failed = 0
        self._last_beat = now
        return line

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
    calendar_service: Any = None     # raw Calendar API resource, for ingestion
    calendar_watch_state: Any = None  # ingestion.calendar_watch.ChannelState
    calendar_sync_state: Any = None   # ingestion.calendar_sync.SyncState
    pending: Any = None              # orchestrator.pending.PendingApprovals
    conversation: Any = None         # conversation.ConversationLog

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
            pending=self.pending,
        )

    def process_chat_event(self, event: dict[str, Any]) -> None:
        """Decode one Chat space event and post the reply back to that space."""
        if self.gchat is None:
            return

        def _post_text(text: str) -> None:
            self.gchat.post_text(self.settings.chat_default_space, text)

        def _brief_fn() -> str:
            return _assemble_runtime_brief(
                self.connector, self.app, self.settings
            ).summary

        handle_chat_message(
            self.app,
            event,
            post_text=_post_text,
            user_id=self.settings.user_id,
            brief_fn=_brief_fn,
            conversation=self.conversation,
        )

    def process_calendar_notification(self, notification: dict[str, Any]):
        """Reconcile one decoded Calendar webhook notification (already
        validated and republished by the thin republisher, per rule 5),
        check each changed event for a scheduling conflict, and notify every
        configured channel about any conflicts found.

        ``notification`` is whatever ``decode_calendar_headers`` produced.
        Recovery from an expired/missing sync token (a full resync, not a
        watch renewal — see ``dispatcher.handle_calendar_notification``'s
        docstring for why those differ) happens inside the dispatcher call.
        """

        def _notify(text: str) -> None:
            if self.slack_say is not None:
                self.slack_say(text=text)
            if self.gchat is not None and self.settings.chat_default_space:
                self.gchat.post_text(self.settings.chat_default_space, text)

        return handle_calendar_notification(
            self.app,
            notification,
            calendar_service=self.calendar_service,
            calendar_sync_state=self.calendar_sync_state,
            connector=self.connector,
            notify=_notify,
            user_id=self.settings.user_id,
            calendar_id=self.settings.calendar_id,
            audit_log=self.app.audit_log,
        )

    def process_chat_interaction(self, event: dict[str, Any]) -> None:
        """Process one decoded Chat card-click event (approve/reject/edit-
        submit — the edit dialog's *open* click is handled synchronously by
        the republisher and never reaches this path). This is the async half of Chat's
        approval flow (see ``docs/decisions.md``): the public webhook
        endpoint never touches the checkpointer itself, it only forwards the
        verified, decoded click here over Pub/Sub."""
        if self.gchat is None:
            return

        def _resume_fn(thread_id: str, decision: str, text: str | None) -> Any:
            return resume_workflow(
                self.app.graph, thread_id, decision, text, pending=self.pending
            )

        def _post_text(text: str) -> None:
            self.gchat.post_text(self.settings.chat_default_space, text)

        handle_chat_interaction(
            self.app,
            event,
            resume_fn=_resume_fn,
            post_text=_post_text,
            user_id=self.settings.user_id,
            audit_log=self.app.audit_log,
        )

    def post_brief(self) -> Any:
        """Assemble one morning brief and post it to every configured channel
        (the Phase-0 deliverable — until the scheduler, nothing ever called
        this). Returns the Brief for callers that want the text."""
        brief = _assemble_runtime_brief(self.connector, self.app, self.settings)
        if self.slack is not None and self.slack_say is not None:
            self.slack.post_brief(self.slack_say, brief)
        if self.gchat is not None and self.settings.chat_default_space:
            self.gchat.post_brief(self.settings.chat_default_space, brief)
        return brief

    def renew_all_watches(self) -> dict[str, str]:
        """Run every configured watch/subscription renewal, isolating and
        auditing each — renewals are exactly the silent-failure class the
        audit log exists for (a lapsed Gmail watch doesn't error, mail just
        quietly stops arriving). Returns {name: "renewed" | "failed: ..."}.
        """
        renewals: list[tuple[str, Any]] = []
        if self.settings.gmail_pubsub_topic:
            renewals.append(("gmail_watch", self.renew_gmail_watch))
        if self.settings.chat_pubsub_topic and self.settings.chat_default_space:
            renewals.append(("chat_subscription", self.renew_chat_subscription))
        if self.settings.calendar_webhook_address:
            renewals.append(("calendar_watch", self.renew_calendar_watch))

        results: dict[str, str] = {}
        for name, renew in renewals:
            try:
                renew()
                results[name] = "renewed"
                event = {"event": "watch_renewed", "target": name}
            except Exception as exc:  # noqa: BLE001 — one failure must not skip the rest
                results[name] = f"failed: {type(exc).__name__}"
                event = {
                    "event": "renewal_failed",
                    "target": name,
                    "error": type(exc).__name__,
                }
            from datetime import datetime, timezone as _tz

            event["ts"] = datetime.now(_tz.utc).isoformat()
            self.app.audit_log.record(
                thread_id=f"ops:renewal:{name}",
                workflow="ops",
                events=[event],
                domain="ops",
                user_id=self.settings.user_id,
            )
        return results

    def run_consolidation(self) -> Any:
        """Nightly memory-consolidation pass (design 2.2). The base substrate
        implementation is currently a no-op report; this gives it its cadence
        so the real pass (roadmap prompt 13) lands with a caller already in
        place. The report is audited either way."""
        report = self.app.store.consolidate(user_id=self.settings.user_id)
        from datetime import datetime, timezone as _tz

        self.app.audit_log.record(
            thread_id="ops:consolidation",
            workflow="ops",
            events=[{
                "event": "consolidation_ran",
                "ts": datetime.now(_tz.utc).isoformat(),
                "merged": getattr(report, "merged", 0),
                "superseded": getattr(report, "superseded", 0),
            }],
            domain="ops",
            user_id=self.settings.user_id,
        )
        return report

    def build_scheduler(self) -> Any:
        """Assemble the standard job set from settings (roadmap prompt 05):
        daily brief at ``brief_time``, daily watch renewals, 6-hourly pending
        sweep, nightly consolidation at ``consolidate_time`` — all in
        ``settings.timezone`` where a wall-clock time is involved."""
        from .scheduler import Job, Scheduler, daily_at, every

        tz = self.settings.timezone
        scheduler = Scheduler()
        if (self.slack is not None and self.slack_say is not None) or (
            self.gchat is not None and self.settings.chat_default_space
        ):
            scheduler.add(
                Job("daily_brief", daily_at(self.settings.brief_time, tz), self.post_brief)
            )
        scheduler.add(Job("renew_watches", every(hours=24), self.renew_all_watches))
        if self.pending is not None:
            scheduler.add(
                Job("sweep_pending", every(hours=6), self.sweep_pending_ignored)
            )
        scheduler.add(
            Job(
                "consolidate",
                daily_at(self.settings.consolidate_time, tz),
                self.run_consolidation,
            )
        )
        return scheduler

    def sweep_pending_ignored(self, *, now: Any = None) -> int:
        """Turn stale unanswered approval cards into IGNORED memory signals
        (design 2.2). Called on a schedule; safe to call any time — each
        entry is swept at most once. Returns how many were swept."""
        if self.pending is None:
            return 0
        from datetime import timedelta

        return sweep_ignored(
            self.pending,
            self.app.store,
            user_id=self.settings.user_id,
            max_age=timedelta(hours=self.settings.approval_ignore_hours),
            now=now,
            audit_log=self.app.audit_log,
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

    def renew_calendar_watch(self, *, force: bool = False):
        return ensure_calendar_watch(
            self.calendar_service,
            self.calendar_watch_state,
            calendar_id=self.settings.calendar_id,
            address=self.settings.calendar_webhook_address,
            force=force,
        )

    # --- supervised pull-loop machinery (testable per-message core) ---------

    def _handle_gmail_message(self, payload: dict[str, Any]) -> None:
        """One decoded Gmail notification, preserving the HistoryExpired
        special case: an expired baseline forces a watch re-registration
        (which re-baselines it) rather than counting as a failure."""
        try:
            self.process_gmail_notification(payload)
        except HistoryExpired:
            self.renew_gmail_watch(force=True)

    def _handle_pulled_message(
        self,
        name: str,
        raw_data: bytes,
        message_id: str,
        handler: Callable[[dict[str, Any]], Any],
    ) -> bool:
        """Decode and dispatch one pulled Pub/Sub message. Never raises.

        Returns True when handled, False for a poison message (malformed
        payload or a raising handler). Either way the caller acks: Pub/Sub
        redelivery of a deterministic failure is an infinite loop, so a
        poison message is logged (by id — never its payload, rule 6),
        audited under the ops workflow, and dropped.
        """
        try:
            payload = json.loads(raw_data)
        except (ValueError, UnicodeDecodeError):
            logger.warning(
                "%s: dropping malformed message id=%s (not JSON)", name, message_id
            )
            self._audit_ops(
                event="message_failed", loop=name,
                message_id=message_id, error="malformed_json",
            )
            return False

        try:
            handler(payload)
            return True
        except Exception as exc:  # noqa: BLE001 — supervision is the contract
            logger.warning(
                "%s: handler failed for message id=%s (%s)",
                name, message_id, type(exc).__name__, exc_info=True,
            )
            self._audit_ops(
                event="message_failed", loop=name,
                message_id=message_id, error=type(exc).__name__,
            )
            return False

    def _audit_ops(self, *, event: str, **fields: Any) -> None:
        """Record an operational event so silent drops stay answerable after
        the fact — best-effort: auditing must never take the loop down."""
        try:
            self.app.audit_log.record(
                thread_id=f"ops:{fields.get('loop', 'runtime')}",
                workflow="ops",
                events=[{
                    "event": event,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    **fields,
                }],
                domain="ops",
                user_id=self.settings.user_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning("ops audit record failed", exc_info=True)

    # --- live loops (pragma: no cover — need real GCP/Slack) ----------------

    def _pull_loop(
        self, name: str, subscription: str, handler: Callable[[dict[str, Any]], Any]
    ) -> None:  # pragma: no cover - thin shell; per-message core tested above
        """Shared supervised pull loop: transport errors back off
        exponentially instead of killing the thread; every message is acked
        (poison ones after logging+audit, via _handle_pulled_message); a
        heartbeat line fires every ~5 minutes."""
        from google.cloud import pubsub_v1

        try:
            from google.api_core.exceptions import DeadlineExceeded
        except ImportError:  # very old google-api-core
            DeadlineExceeded = ()  # type: ignore[assignment]

        subscriber = pubsub_v1.SubscriberClient()
        stats = LoopStats(name)
        backoff = BACKOFF_INITIAL_SECONDS
        logger.info("%s: pull loop started (subscription=%s)", name, subscription)
        while True:
            try:
                response = subscriber.pull(
                    request={"subscription": subscription, "max_messages": 10},
                    timeout=30,
                )
            except DeadlineExceeded:
                # An empty pull window is normal idleness, not a failure.
                response = None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "%s: pull failed (%s); backing off %.0fs",
                    name, type(exc).__name__, backoff,
                )
                time.sleep(backoff)
                backoff = next_backoff(backoff)
                continue

            backoff = BACKOFF_INITIAL_SECONDS
            for received in (response.received_messages if response else []):
                ok = self._handle_pulled_message(
                    name,
                    received.message.data,
                    getattr(received.message, "message_id", ""),
                    handler,
                )
                stats.record(ok)
                subscriber.acknowledge(
                    request={
                        "subscription": subscription,
                        "ack_ids": [received.ack_id],
                    }
                )
            beat = stats.maybe_beat()
            if beat:
                logger.info(beat)

    def run_gmail_pubsub_loop(self) -> None:  # pragma: no cover
        """Pull Gmail Pub/Sub notifications forever and dispatch each one."""
        self._pull_loop(
            "gmail",
            self.settings.gmail_pubsub_subscription,
            self._handle_gmail_message,
        )

    def run_chat_pubsub_loop(self) -> None:  # pragma: no cover
        """Pull Chat Workspace Events notifications forever and dispatch each."""
        self._pull_loop(
            "chat", self.settings.chat_pubsub_subscription, self.process_chat_event
        )

    def run_chat_interaction_pubsub_loop(self) -> None:  # pragma: no cover
        """Pull decoded Chat card-click events forever and resume each one.
        The webhook itself (verified by the thin republisher before it ever
        publishes) is received outside this process, same as Calendar's —
        this only pulls the already-verified, already-decoded click off the
        Pub/Sub subscription."""
        self._pull_loop(
            "chat_interaction",
            self.settings.chat_interaction_pubsub_subscription,
            self.process_chat_interaction,
        )

    def run_calendar_pubsub_loop(self) -> None:  # pragma: no cover
        """Pull decoded Calendar webhook notifications forever and reconcile
        each one. The webhook itself (rule 5's one genuine exception) is
        received and republished by an external thin service, not this
        process — this only pulls the republished, already-decoded
        notification off the Pub/Sub subscription."""
        self._pull_loop(
            "calendar",
            self.settings.calendar_pubsub_subscription,
            self.process_calendar_notification,
        )

    def run(self) -> None:  # pragma: no cover
        """Start the always-on process: renew all watches once at startup (a
        fresh deployment must not wait a day for its first registration),
        start the scheduler (brief / renewals / sweep / consolidation) and
        the Gmail/Chat/Calendar pull loops on daemon threads; Slack Socket
        Mode blocks the main thread (or, absent Slack, the main thread just
        waits so the daemon threads keep running)."""
        self.renew_all_watches()
        scheduler = self.build_scheduler()
        threading.Thread(target=scheduler.run_loop, daemon=True).start()

        if self.settings.gmail_pubsub_subscription:
            threading.Thread(target=self.run_gmail_pubsub_loop, daemon=True).start()
        if self.settings.chat_pubsub_subscription and self.gchat is not None:
            threading.Thread(target=self.run_chat_pubsub_loop, daemon=True).start()
        if self.settings.chat_interaction_pubsub_subscription and self.gchat is not None:
            threading.Thread(
                target=self.run_chat_interaction_pubsub_loop, daemon=True
            ).start()
        if self.settings.calendar_pubsub_subscription:
            threading.Thread(target=self.run_calendar_pubsub_loop, daemon=True).start()

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
    calendar_service: Any = None,
    calendar_watch_state: ChannelState | None = None,
    calendar_sync_state: SyncState | None = None,
    pending: Any = None,
    conversation: Any = None,
) -> Runtime:
    """Assemble a :class:`Runtime` from config and optional overrides.

    Mirrors ``app.build_app``'s override-or-build-real pattern: pass fakes for
    every collaborator in tests; in production, omit overrides and each is
    constructed from ``settings``.

    - *credentials* — via ``load_google_credentials(settings)``
    - *connector*   — via ``make_connector(settings, credentials=...)``
    - *gmail_service* / *calendar_service* — raw Gmail/Calendar API resources,
      built from *credentials* (ingestion needs these directly; independent of
      *connector*, which may be the MCP implementation instead)
    - *watch_state* / *chat_state* / *calendar_watch_state* /
      *calendar_sync_state* — ``JsonGmailWatchState`` /
      ``JsonChatSubscriptionState`` / ``JsonCalendarChannelState`` /
      ``JsonCalendarSyncState``, backed by ``settings.*_state_path``
    - *slack* / *gchat* — only built when the relevant tokens/state are
      present in ``settings``; a deployment need not run both channels
    """
    settings = settings or Settings.from_env()

    # Credentials + connector are resolved BEFORE the app so the graph's apply
    # step can be bound to the real connector (approved drafts materialize as
    # Gmail drafts via create_draft — the safe write path, rule 4).
    resolved_credentials = credentials
    if resolved_credentials is None and (
        connector is None or gmail_service is None or calendar_service is None
    ):
        resolved_credentials = load_google_credentials(settings)

    resolved_connector = connector or make_connector(
        settings, credentials=resolved_credentials
    )

    resolved_app = app or build_app(
        settings, apply_fn=make_connector_apply_fn(resolved_connector)
    )

    resolved_pending = pending or JsonPendingApprovals(settings.pending_state_path)
    resolved_conversation = conversation or JsonConversationLog(
        settings.conversation_state_path,
        max_turns=settings.converse_window_turns,
        ttl_minutes=settings.converse_ttl_minutes,
    )

    # The one shared resume path, bound to the pending registry so every
    # decision — whichever channel it arrives on — marks its card resolved.
    def _bound_resume(thread_id: str, decision: str, text: str | None) -> Any:
        return resume_workflow(
            resolved_app.graph, thread_id, decision, text, pending=resolved_pending
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
    resolved_calendar_watch_state = calendar_watch_state or JsonCalendarChannelState(
        settings.calendar_watch_state_path
    )
    resolved_calendar_sync_state = calendar_sync_state or JsonCalendarSyncState(
        settings.calendar_sync_state_path
    )

    resolved_calendar_service = calendar_service
    if resolved_calendar_service is None:  # pragma: no cover - requires live creds
        from googleapiclient.discovery import build as _build

        resolved_calendar_service = _build(
            "calendar", "v3", credentials=resolved_credentials
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
                brief_fn=lambda: _assemble_runtime_brief(
                    resolved_connector, resolved_app, settings
                ).summary,
                conversation=resolved_conversation,
            )

        resolved_slack = SlackChannel(
            graph=resolved_app.graph,
            resume_fn=_bound_resume,
            message_fn=_slack_message_fn,
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
            resume_fn=_bound_resume,
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
        calendar_service=resolved_calendar_service,
        calendar_watch_state=resolved_calendar_watch_state,
        calendar_sync_state=resolved_calendar_sync_state,
        pending=resolved_pending,
        conversation=resolved_conversation,
    )
