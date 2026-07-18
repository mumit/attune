"""Workspace + Slack connectors (design doc 4.3, 4.4, 4.7).

A small internal interface (``WorkspaceConnector``) with two implementations
behind it: configured MCP servers (``McpWorkspaceConnector``) and direct
OAuth (``DirectOAuthConnector``). Which one runs is a config choice
(``config.WorkspaceBackend``) via ``make_connector``.

Safe by construction: every fetched item is provenance-tagged FETCHED
(untrusted) at this boundary, and send is not a default capability — the primary
write is create_draft; sending requires an explicit scope + autonomy grant on the
direct-OAuth path (the MCP path can't send at all).
"""

from .base import (
    CalendarEvent,
    CalendarWriteNotPermitted,
    ConnectorError,
    DEFAULT_NOISE_LABEL,
    DraftRef,
    EmailThread,
    LabelNotPermitted,
    Provenance,
    SendNotPermitted,
    WorkspaceConnector,
)
from .mcp import McpWorkspaceConnector
from .google_oauth import DirectOAuthConnector


def make_connector(settings, **kwargs) -> WorkspaceConnector:
    """Select the connector implementation from a config.Settings.

    MCP mode builds a Streamable HTTP caller unless ``mcp_call`` is injected;
    google_oauth mode accepts ``credentials``, ``send_enabled``,
    ``labels_enabled`` (Phase 3 stage 1, G9 — mirrors ``send_enabled``'s
    double-gate: set only alongside the gmail.modify scope), and
    ``calendar_writes_enabled`` (Phase 3 stage 2 — same discipline, set only
    alongside the calendar.events scope).
    """
    from ..config import WorkspaceBackend

    if settings.connector_mode == WorkspaceBackend.MCP:
        mcp_call = kwargs.get("mcp_call")
        if mcp_call is None:
            from .mcp_client import make_mcp_caller

            mcp_call = make_mcp_caller(settings)
        return McpWorkspaceConnector(
            mcp_call, internal_domains=settings.internal_domains
        )
    user = getattr(settings, "user_id", "") or ""
    return DirectOAuthConnector(
        credentials=kwargs.get("credentials"),
        gmail_service=kwargs.get("gmail_service"),
        calendar_service=kwargs.get("calendar_service"),
        send_enabled=kwargs.get("send_enabled", False),
        labels_enabled=kwargs.get("labels_enabled", False),
        calendar_writes_enabled=kwargs.get("calendar_writes_enabled", False),
        owner_email=user if "@" in user else None,
        internal_domains=settings.internal_domains,
    )


__all__ = [
    "WorkspaceConnector",
    "EmailThread",
    "CalendarEvent",
    "DraftRef",
    "Provenance",
    "ConnectorError",
    "SendNotPermitted",
    "LabelNotPermitted",
    "CalendarWriteNotPermitted",
    "DEFAULT_NOISE_LABEL",
    "McpWorkspaceConnector",
    "DirectOAuthConnector",
    "make_connector",
]
