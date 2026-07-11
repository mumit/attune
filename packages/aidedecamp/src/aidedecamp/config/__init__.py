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
        return cls(
            deployment=Deployment(e.get("ADC_DEPLOYMENT", "personal")),
            connector_mode=ConnectorMode(e.get("ADC_CONNECTOR_MODE", "mcp")),
            mem0_url=e.get("ADC_MEM0_URL", "http://localhost:8000"),
            audit_log_path=e.get("ADC_AUDIT_LOG_PATH", "./audit.log.jsonl"),
            slack_app_token=e.get("SLACK_APP_TOKEN"),
            slack_bot_token=e.get("SLACK_BOT_TOKEN"),
            google_project_id=e.get("GOOGLE_PROJECT_ID"),
            google_credentials_file=e.get("ADC_GOOGLE_CREDENTIALS_FILE"),
            gmail_pubsub_topic=e.get("ADC_GMAIL_PUBSUB_TOPIC"),
            gmail_pubsub_subscription=e.get("ADC_GMAIL_PUBSUB_SUBSCRIPTION"),
            chat_pubsub_topic=e.get("ADC_CHAT_PUBSUB_TOPIC"),
            chat_pubsub_subscription=e.get("ADC_CHAT_PUBSUB_SUBSCRIPTION"),
            calendar_pubsub_topic=e.get("ADC_CALENDAR_PUBSUB_TOPIC"),
            calendar_pubsub_subscription=e.get("ADC_CALENDAR_PUBSUB_SUBSCRIPTION"),
            calendar_webhook_address=e.get("ADC_CALENDAR_WEBHOOK_ADDRESS"),
            calendar_id=e.get("ADC_CALENDAR_ID", "primary"),
            checkpointer_db_path=e.get("ADC_DB_PATH", "./aidedecamp.db"),
            gmail_watch_state_path=e.get(
                "ADC_GMAIL_WATCH_STATE_PATH", "./gmail_watch_state.json"
            ),
            chat_subscription_state_path=e.get(
                "ADC_CHAT_SUBSCRIPTION_STATE_PATH", "./chat_subscription_state.json"
            ),
            calendar_watch_state_path=e.get(
                "ADC_CALENDAR_WATCH_STATE_PATH", "./calendar_watch_state.json"
            ),
            calendar_sync_state_path=e.get(
                "ADC_CALENDAR_SYNC_STATE_PATH", "./calendar_sync_state.json"
            ),
            user_id=e.get("ADC_USER_ID", "me"),
            slack_default_channel=e.get("ADC_SLACK_CHANNEL"),
            chat_default_space=e.get("ADC_CHAT_SPACE"),
        )
