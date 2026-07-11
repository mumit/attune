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
    checkpointer_db_path: str = "./aidedecamp.db"
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
            checkpointer_db_path=e.get("ADC_DB_PATH", "./aidedecamp.db"),
        )
