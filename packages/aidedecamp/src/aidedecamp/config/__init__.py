"""Runtime configuration for a single Aide-de-camp deployment.

Per the design doc (4.6, 4.7), personal and TELUS run as two *separate*
deployments of this same codebase, each with its own credentials, memory store,
and audit log. That separation is expressed as configuration, not as branching
logic inside the code. This module loads that configuration from the
environment so the same container image runs in both places with different env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum


class Deployment(str, Enum):
    PERSONAL = "personal"
    TELUS = "telus"


class IngestionMode(str, Enum):
    """How events arrive. ``POLL`` (the default) drives the same
    reconciliation code from a timer — outbound-only, zero Pub/Sub, zero
    republisher, the day-one path. ``PUSH`` is the hardened production
    posture: Pub/Sub pull subscriptions fed by watches + the republisher.
    The dispatcher never learns which mode fed it."""

    POLL = "poll"
    PUSH = "push"


class ConnectorMode(str, Enum):
    """How Workspace is reached. Chosen per deployment (design doc 4.3, 4.7):
    the personal side can default to Google's managed MCP servers; the TELUS
    side uses whichever clears governance review. A 'no' from TELUS IT on MCP
    is a config change here, not a redesign."""

    MCP = "mcp"
    DIRECT_OAUTH = "direct_oauth"


@dataclass(frozen=True)
class Settings:
    """Resolved settings for one deployment. Construct via ``Settings.from_env``."""

    deployment: Deployment
    connector_mode: ConnectorMode

    # Fuel iX token env var name is handled in fuelix.py (FUELIX_TOKEN); the
    # token value itself is never stored here or logged.
    mem0_url: str
    audit_log_path: str

    # One directory for all mutable state (ADC_DATA_DIR). When set, every
    # *_path/*_db default below derives from it — collapsing eight path vars
    # into one for new users — while an explicit per-path env var still wins.
    data_dir: str | None = None

    # Optional, populated per channel/source as they come online.
    slack_app_token: str | None = None
    slack_bot_token: str | None = None
    google_project_id: str | None = None
    # Path to a Google credentials JSON file (service account or OAuth user).
    # When absent, google.auth.default() (ADC) is used.
    google_credentials_file: str | None = None
    # Fully-qualified Pub/Sub topic names (what users.watch/subscriptions.create
    # publish to) and subscription names (what the runtime pulls from — a
    # separate GCP resource attached to the topic) for Gmail and Chat ingestion.
    gmail_pubsub_topic: str | None = None
    gmail_pubsub_subscription: str | None = None
    chat_pubsub_topic: str | None = None
    chat_pubsub_subscription: str | None = None
    # Chat card-click interactions (approve/reject/edit-submit — the edit
    # dialog's *open* click is handled synchronously by the republisher and
    # never reaches this topic) republished the same way Calendar's webhook is, since Chat
    # interactivity also requires a synchronous HTTP response the credential-
    # holding process must never provide directly (rule 5).
    chat_interaction_pubsub_topic: str | None = None
    chat_interaction_pubsub_subscription: str | None = None
    # Calendar has no Pub/Sub option (design 4.6) — Google POSTs directly to
    # calendar_webhook_address, a thin external republisher which forwards a
    # decoded notification onto this topic/subscription instead, so this
    # process still never opens an inbound port (rule 5).
    calendar_pubsub_topic: str | None = None
    calendar_pubsub_subscription: str | None = None
    calendar_webhook_address: str | None = None
    calendar_id: str = "primary"
    checkpointer_db_path: str = "./aidedecamp.db"
    # Where the ingestion watch/subscription baselines persist between restarts.
    gmail_watch_state_path: str = "./gmail_watch_state.json"
    chat_subscription_state_path: str = "./chat_subscription_state.json"
    calendar_watch_state_path: str = "./calendar_watch_state.json"
    calendar_sync_state_path: str = "./calendar_sync_state.json"
    # Pending-approval registry (card dedupe + the IGNORED-signal sweep).
    pending_state_path: str = "./pending_approvals.json"
    # How long a card sits unanswered before the sweep captures IGNORED.
    approval_ignore_hours: int = 48
    # Conversational Q&A working memory: rolling window per (channel, user).
    conversation_state_path: str = "./conversation_state.json"
    converse_window_turns: int = 10
    converse_ttl_minutes: int = 120
    # Scheduler cadences. timezone is an IANA name; brief/consolidate times
    # are local wall-clock "HH:MM" in that timezone.
    timezone: str = "UTC"
    brief_time: str = "07:30"
    consolidate_time: str = "02:00"
    # Ingestion: poll (default, no GCP infra) vs push (Pub/Sub + republisher).
    ingestion_mode: IngestionMode = IngestionMode.POLL
    # Poll cadence; floored at 30s (Google quota concern, CLAUDE.md).
    poll_seconds: int = 120
    # Chat poll high-water mark (poll mode only).
    chat_poll_state_path: str = "./chat_poll_state.json"
    # Persisted autonomy grants (the permission matrix). Written only by
    # explicit grant/revoke operations, loaded by build_app.
    autonomy_state_path: str = "./autonomy_grants.json"
    # The single identity this deployment acts as (memory/audit user_id, and
    # the Gmail API "me" alias). One deployment = one identity, per design 4.6.
    user_id: str = "me"
    # Where the assistant proactively posts (briefs, approval cards) absent a
    # live event context to reply into.
    slack_default_channel: str | None = None
    chat_default_space: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = env if env is not None else dict(os.environ)
        # expanduser so a hand-edited `ADC_DATA_DIR=~/.aidedecamp` works.
        data_dir = os.path.expanduser(e.get("ADC_DATA_DIR") or "") or None

        def _path(key: str, filename: str) -> str:
            """Explicit env var wins; else derive from data_dir; else CWD."""
            explicit = e.get(key)
            if explicit:
                return explicit
            if data_dir:
                return os.path.join(data_dir, filename)
            return f"./{filename}"

        return cls(
            deployment=Deployment(e.get("ADC_DEPLOYMENT", "personal")),
            connector_mode=ConnectorMode(e.get("ADC_CONNECTOR_MODE", "mcp")),
            mem0_url=e.get("ADC_MEM0_URL", "http://localhost:8000"),
            data_dir=data_dir,
            audit_log_path=_path("ADC_AUDIT_LOG_PATH", "audit.log.jsonl"),
            slack_app_token=e.get("SLACK_APP_TOKEN"),
            slack_bot_token=e.get("SLACK_BOT_TOKEN"),
            google_project_id=e.get("GOOGLE_PROJECT_ID"),
            google_credentials_file=e.get("ADC_GOOGLE_CREDENTIALS_FILE"),
            gmail_pubsub_topic=e.get("ADC_GMAIL_PUBSUB_TOPIC"),
            gmail_pubsub_subscription=e.get("ADC_GMAIL_PUBSUB_SUBSCRIPTION"),
            chat_pubsub_topic=e.get("ADC_CHAT_PUBSUB_TOPIC"),
            chat_pubsub_subscription=e.get("ADC_CHAT_PUBSUB_SUBSCRIPTION"),
            chat_interaction_pubsub_topic=e.get("ADC_CHAT_INTERACTION_PUBSUB_TOPIC"),
            chat_interaction_pubsub_subscription=e.get(
                "ADC_CHAT_INTERACTION_PUBSUB_SUBSCRIPTION"
            ),
            calendar_pubsub_topic=e.get("ADC_CALENDAR_PUBSUB_TOPIC"),
            calendar_pubsub_subscription=e.get("ADC_CALENDAR_PUBSUB_SUBSCRIPTION"),
            calendar_webhook_address=e.get("ADC_CALENDAR_WEBHOOK_ADDRESS"),
            calendar_id=e.get("ADC_CALENDAR_ID", "primary"),
            checkpointer_db_path=_path("ADC_DB_PATH", "aidedecamp.db"),
            gmail_watch_state_path=_path("ADC_GMAIL_WATCH_STATE_PATH", "gmail_watch_state.json"),
            chat_subscription_state_path=_path("ADC_CHAT_SUBSCRIPTION_STATE_PATH", "chat_subscription_state.json"),
            calendar_watch_state_path=_path("ADC_CALENDAR_WATCH_STATE_PATH", "calendar_watch_state.json"),
            calendar_sync_state_path=_path("ADC_CALENDAR_SYNC_STATE_PATH", "calendar_sync_state.json"),
            pending_state_path=_path("ADC_PENDING_STATE_PATH", "pending_approvals.json"),
            approval_ignore_hours=int(e.get("ADC_APPROVAL_IGNORE_HOURS", "48")),
            conversation_state_path=_path("ADC_CONVERSATION_STATE_PATH", "conversation_state.json"),
            converse_window_turns=int(e.get("ADC_CONVERSE_WINDOW_TURNS", "10")),
            converse_ttl_minutes=int(e.get("ADC_CONVERSE_TTL_MINUTES", "120")),
            timezone=e.get("ADC_TIMEZONE", "UTC"),
            brief_time=e.get("ADC_BRIEF_TIME", "07:30"),
            consolidate_time=e.get("ADC_CONSOLIDATE_TIME", "02:00"),
            ingestion_mode=IngestionMode(e.get("ADC_INGESTION_MODE", "poll")),
            poll_seconds=max(int(e.get("ADC_POLL_SECONDS", "120")), 30),
            chat_poll_state_path=_path(
                "ADC_CHAT_POLL_STATE_PATH", "chat_poll_state.json"
            ),
            autonomy_state_path=_path(
                "ADC_AUTONOMY_STATE_PATH", "autonomy_grants.json"
            ),
            user_id=e.get("ADC_USER_ID", "me"),
            slack_default_channel=e.get("ADC_SLACK_CHANNEL"),
            chat_default_space=e.get("ADC_CHAT_SPACE"),
        )
