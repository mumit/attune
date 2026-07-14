"""Environment-backed configuration for one Attune instance.

An instance represents one principal and owns its credentials, memory, state,
and audit log.  Where it is hosted is deliberately not part of application
configuration.  Google-specific event transports remain explicit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum


def _csv_set(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _is_true(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


class WorkspaceBackend(str, Enum):
    GOOGLE_OAUTH = "google_oauth"
    MCP = "mcp"


# Compatibility import for callers written before the Attune rename.
ConnectorMode = WorkspaceBackend


class IngestionMode(str, Enum):
    POLL = "poll"
    GOOGLE_PUBSUB = "google_pubsub"

    # Compatibility member for older code/tests. Its value is the honest,
    # provider-specific spelling used in new configuration.
    PUSH = "google_pubsub"


CHANNEL_NAMES = frozenset({"slack", "google_chat"})


@dataclass(frozen=True)
class Settings:
    workspace_backend: WorkspaceBackend
    ingestion_mode: IngestionMode

    # OpenAI-compatible Chat Completions gateway. The SDK sends api_key as a
    # bearer credential. Models are selected by semantic task in llm.py.
    llm_base_url: str
    llm_api_key: str | None
    model_default: str | None
    model_classify: str | None
    model_draft: str | None
    model_reason: str | None
    model_consolidate: str | None
    model_converse: str | None
    model_memory_extract: str | None
    embedding_base_url: str
    embedding_api_key: str | None
    embedding_model: str | None
    embedding_dimensions: int | None

    mem0_url: str
    audit_log_path: str
    data_dir: str | None = None

    # Principal and organization boundary.
    user_id: str = "me"
    internal_domains: frozenset[str] = frozenset()

    # Direct Google OAuth backend.
    google_project_id: str | None = None
    google_credentials_file: str | None = None

    # MCP backend (Streamable HTTP). Separate URLs support managed Gmail and
    # Calendar servers; ATTUNE_MCP_URL is the shared fallback.
    mcp_url: str | None = None
    mcp_gmail_url: str | None = None
    mcp_calendar_url: str | None = None
    mcp_token: str | None = None

    # Optional channel credentials and routing.
    slack_app_token: str | None = None
    slack_bot_token: str | None = None
    chat_credentials_file: str | None = None
    brief_channels: frozenset[str] = frozenset()
    approval_channel: str | None = None
    notification_channels: frozenset[str] = frozenset()
    interaction_channels: frozenset[str] = frozenset()

    gmail_pubsub_topic: str | None = None
    gmail_pubsub_subscription: str | None = None
    chat_pubsub_topic: str | None = None
    chat_pubsub_subscription: str | None = None
    chat_interaction_pubsub_topic: str | None = None
    chat_interaction_pubsub_subscription: str | None = None
    calendar_pubsub_topic: str | None = None
    calendar_pubsub_subscription: str | None = None
    calendar_webhook_address: str | None = None
    calendar_id: str = "primary"

    checkpointer_db_path: str = "./attune.db"
    gmail_watch_state_path: str = "./gmail_watch_state.json"
    chat_subscription_state_path: str = "./chat_subscription_state.json"
    calendar_watch_state_path: str = "./calendar_watch_state.json"
    calendar_sync_state_path: str = "./calendar_sync_state.json"
    pending_state_path: str = "./pending_approvals.json"
    conversation_state_path: str = "./conversation_state.json"
    chat_poll_state_path: str = "./chat_poll_state.json"
    connector_poll_state_path: str = "./workspace_poll_state.json"
    autonomy_state_path: str = "./autonomy_grants.json"
    nudge_state_path: str = "./nudge_state.json"
    retry_queue_db_path: str = "./source_retries.db"

    approval_ignore_hours: int = 48
    converse_window_turns: int = 10
    converse_ttl_minutes: int = 120
    timezone: str = "UTC"
    brief_time: str = "07:30"
    consolidate_time: str = "02:00"
    poll_seconds: int = 120
    nudge_time: str = "14:00"
    nudge_min_age_days: int = 4
    nudge_cooldown_days: int = 7

    slack_allowed_users: frozenset[str] = frozenset()
    chat_allowed_users: frozenset[str] = frozenset()
    destination_visibility_acknowledged: bool = False
    slack_default_channel: str | None = None
    chat_default_space: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def connector_mode(self) -> WorkspaceBackend:
        """Compatibility spelling for the connector factory."""
        return self.workspace_backend

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        e = env if env is not None else dict(os.environ)
        data_dir = os.path.expanduser(e.get("ATTUNE_DATA_DIR") or "") or None

        def _path(key: str, filename: str) -> str:
            explicit = e.get(key)
            if explicit:
                return os.path.expanduser(explicit)
            if data_dir:
                return os.path.join(data_dir, filename)
            return f"./{filename}"

        def _model(name: str) -> str | None:
            return e.get(f"ATTUNE_MODEL_{name}") or e.get("ATTUNE_MODEL_DEFAULT")

        user_id = e.get("ATTUNE_USER_ID", "me")
        domains = _csv_set(e.get("ATTUNE_INTERNAL_DOMAINS"))
        if not domains and "@" in user_id:
            domains = frozenset({user_id.rsplit("@", 1)[1].lower()})

        raw_ingestion = e.get("ATTUNE_INGESTION_MODE", "poll")
        if raw_ingestion == "push":  # migration compatibility after init edit
            raw_ingestion = "google_pubsub"

        llm_base = (e.get("ATTUNE_LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        embed_base = (e.get("ATTUNE_EMBEDDING_BASE_URL") or llm_base).rstrip("/")
        dims = e.get("ATTUNE_EMBEDDING_DIMENSIONS")
        inferred_channels: list[str] = []
        if e.get("SLACK_BOT_TOKEN") or e.get("ATTUNE_SLACK_CHANNEL"):
            inferred_channels.append("slack")
        if e.get("ATTUNE_CHAT_SPACE"):
            inferred_channels.append("google_chat")
        inferred = frozenset(inferred_channels)

        return cls(
            workspace_backend=WorkspaceBackend(
                e.get("ATTUNE_WORKSPACE_BACKEND", "google_oauth")
            ),
            ingestion_mode=IngestionMode(raw_ingestion),
            llm_base_url=llm_base,
            llm_api_key=e.get("ATTUNE_LLM_API_KEY"),
            model_default=e.get("ATTUNE_MODEL_DEFAULT"),
            model_classify=_model("CLASSIFY"),
            model_draft=_model("DRAFT"),
            model_reason=_model("REASON"),
            model_consolidate=_model("CONSOLIDATE"),
            model_converse=_model("CONVERSE"),
            model_memory_extract=_model("MEMORY_EXTRACT") or _model("CLASSIFY"),
            embedding_base_url=embed_base,
            embedding_api_key=e.get("ATTUNE_EMBEDDING_API_KEY") or e.get("ATTUNE_LLM_API_KEY"),
            embedding_model=e.get("ATTUNE_EMBEDDING_MODEL"),
            embedding_dimensions=int(dims) if dims else None,
            mem0_url=e.get("ATTUNE_MEM0_URL", "http://localhost:8000"),
            data_dir=data_dir,
            audit_log_path=_path("ATTUNE_AUDIT_LOG_PATH", "audit.log.jsonl"),
            user_id=user_id,
            internal_domains=domains,
            google_project_id=e.get("GOOGLE_PROJECT_ID"),
            google_credentials_file=e.get("ATTUNE_GOOGLE_CREDENTIALS_FILE"),
            mcp_url=e.get("ATTUNE_MCP_URL"),
            mcp_gmail_url=e.get("ATTUNE_MCP_GMAIL_URL"),
            mcp_calendar_url=e.get("ATTUNE_MCP_CALENDAR_URL"),
            mcp_token=e.get("ATTUNE_MCP_TOKEN"),
            slack_app_token=e.get("SLACK_APP_TOKEN"),
            slack_bot_token=e.get("SLACK_BOT_TOKEN"),
            chat_credentials_file=e.get("ATTUNE_CHAT_CREDENTIALS_FILE"),
            brief_channels=(
                _csv_set(e.get("ATTUNE_BRIEF_CHANNELS"))
                if "ATTUNE_BRIEF_CHANNELS" in e else inferred
            ),
            approval_channel=(
                e.get("ATTUNE_APPROVAL_CHANNEL") or None
                if "ATTUNE_APPROVAL_CHANNEL" in e
                else (inferred_channels[0] if inferred_channels else None)
            ),
            notification_channels=(
                _csv_set(e.get("ATTUNE_NOTIFICATION_CHANNELS"))
                if "ATTUNE_NOTIFICATION_CHANNELS" in e else inferred
            ),
            interaction_channels=(
                _csv_set(e.get("ATTUNE_INTERACTION_CHANNELS"))
                if "ATTUNE_INTERACTION_CHANNELS" in e else inferred
            ),
            gmail_pubsub_topic=e.get("ATTUNE_GMAIL_PUBSUB_TOPIC"),
            gmail_pubsub_subscription=e.get("ATTUNE_GMAIL_PUBSUB_SUBSCRIPTION"),
            chat_pubsub_topic=e.get("ATTUNE_CHAT_PUBSUB_TOPIC"),
            chat_pubsub_subscription=e.get("ATTUNE_CHAT_PUBSUB_SUBSCRIPTION"),
            chat_interaction_pubsub_topic=e.get("ATTUNE_CHAT_INTERACTION_PUBSUB_TOPIC"),
            chat_interaction_pubsub_subscription=e.get("ATTUNE_CHAT_INTERACTION_PUBSUB_SUBSCRIPTION"),
            calendar_pubsub_topic=e.get("ATTUNE_CALENDAR_PUBSUB_TOPIC"),
            calendar_pubsub_subscription=e.get("ATTUNE_CALENDAR_PUBSUB_SUBSCRIPTION"),
            calendar_webhook_address=e.get("ATTUNE_CALENDAR_WEBHOOK_ADDRESS"),
            calendar_id=e.get("ATTUNE_CALENDAR_ID", "primary"),
            checkpointer_db_path=_path("ATTUNE_DB_PATH", "attune.db"),
            gmail_watch_state_path=_path("ATTUNE_GMAIL_WATCH_STATE_PATH", "gmail_watch_state.json"),
            chat_subscription_state_path=_path("ATTUNE_CHAT_SUBSCRIPTION_STATE_PATH", "chat_subscription_state.json"),
            calendar_watch_state_path=_path("ATTUNE_CALENDAR_WATCH_STATE_PATH", "calendar_watch_state.json"),
            calendar_sync_state_path=_path("ATTUNE_CALENDAR_SYNC_STATE_PATH", "calendar_sync_state.json"),
            pending_state_path=_path("ATTUNE_PENDING_STATE_PATH", "pending_approvals.json"),
            conversation_state_path=_path("ATTUNE_CONVERSATION_STATE_PATH", "conversation_state.json"),
            chat_poll_state_path=_path("ATTUNE_CHAT_POLL_STATE_PATH", "chat_poll_state.json"),
            connector_poll_state_path=_path("ATTUNE_CONNECTOR_POLL_STATE_PATH", "workspace_poll_state.json"),
            autonomy_state_path=_path("ATTUNE_AUTONOMY_STATE_PATH", "autonomy_grants.json"),
            nudge_state_path=_path("ATTUNE_NUDGE_STATE_PATH", "nudge_state.json"),
            retry_queue_db_path=_path("ATTUNE_RETRY_QUEUE_DB_PATH", "source_retries.db"),
            approval_ignore_hours=int(e.get("ATTUNE_APPROVAL_IGNORE_HOURS", "48")),
            converse_window_turns=int(e.get("ATTUNE_CONVERSE_WINDOW_TURNS", "10")),
            converse_ttl_minutes=int(e.get("ATTUNE_CONVERSE_TTL_MINUTES", "120")),
            timezone=e.get("ATTUNE_TIMEZONE", "UTC"),
            brief_time=e.get("ATTUNE_BRIEF_TIME", "07:30"),
            consolidate_time=e.get("ATTUNE_CONSOLIDATE_TIME", "02:00"),
            poll_seconds=max(int(e.get("ATTUNE_POLL_SECONDS", "120")), 30),
            nudge_time=e.get("ATTUNE_NUDGE_TIME", "14:00"),
            nudge_min_age_days=int(e.get("ATTUNE_NUDGE_MIN_AGE_DAYS", "4")),
            nudge_cooldown_days=int(e.get("ATTUNE_NUDGE_COOLDOWN_DAYS", "7")),
            slack_allowed_users=_csv_set(e.get("ATTUNE_SLACK_ALLOWED_USERS")),
            chat_allowed_users=_csv_set(e.get("ATTUNE_CHAT_ALLOWED_USERS")),
            destination_visibility_acknowledged=_is_true(e.get("ATTUNE_ACK_DESTINATION_VISIBILITY")),
            slack_default_channel=e.get("ATTUNE_SLACK_CHANNEL"),
            chat_default_space=e.get("ATTUNE_CHAT_SPACE"),
        )

    def validate(self) -> None:
        if self.workspace_backend == WorkspaceBackend.MCP:
            if self.ingestion_mode != IngestionMode.POLL:
                raise ValueError("MCP workspace backend currently requires ATTUNE_INGESTION_MODE=poll")
            if not (self.mcp_url or (self.mcp_gmail_url and self.mcp_calendar_url)):
                raise ValueError("MCP backend requires ATTUNE_MCP_URL or both service-specific MCP URLs")
        for name in self.brief_channels | self.notification_channels | self.interaction_channels:
            if name not in CHANNEL_NAMES:
                raise ValueError(f"unknown channel {name!r}; expected slack or google_chat")
        if self.approval_channel and self.approval_channel not in CHANNEL_NAMES:
            raise ValueError("ATTUNE_APPROVAL_CHANNEL must be slack or google_chat")
        self.validate_proactive_destinations()

    def validate_proactive_destinations(self) -> None:
        if self.slack_default_channel and not self.slack_default_channel.startswith(("D", "C", "G")):
            raise ValueError("ATTUNE_SLACK_CHANNEL must be a Slack conversation ID beginning D, C, or G")
        if self.chat_default_space and not self.chat_default_space.startswith("spaces/"):
            raise ValueError("ATTUNE_CHAT_SPACE must be a resource name such as spaces/AAAA")
        slack_needs_ack = bool(self.slack_default_channel and not self.slack_default_channel.startswith("D"))
        chat_needs_ack = bool(self.chat_default_space)
        if (slack_needs_ack or chat_needs_ack) and not self.destination_visibility_acknowledged:
            raise ValueError(
                "proactive destination visibility is unverified; use an owner-only Slack DM or set ATTUNE_ACK_DESTINATION_VISIBILITY=1"
            )
