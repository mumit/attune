"""Workspace + Slack connectors (design doc 4.3, 4.4, 4.7).

A small internal interface (``WorkspaceConnector``) with two implementations
behind it: managed Google MCP servers (``McpWorkspaceConnector``) and direct
OAuth (``DirectOAuthConnector``). Which one runs is a config choice
(``config.ConnectorMode``) via ``make_connector``, so a TELUS "no" on MCP is not
a rewrite.

Safe by construction: every fetched item is provenance-tagged FETCHED
(untrusted) at this boundary, and send is not a default capability — the primary
write is create_draft; sending requires an explicit scope + autonomy grant on the
direct-OAuth path (the MCP path can't send at all).
"""

from .base import (
    CalendarEvent,
    ConnectorError,
    DraftRef,
    EmailThread,
    Provenance,
    SendNotPermitted,
    WorkspaceConnector,
)
from .mcp import McpWorkspaceConnector
from .direct_oauth import DirectOAuthConnector


def make_connector(settings, **kwargs) -> WorkspaceConnector:
    """Select the connector implementation from a config.Settings.

    MCP mode requires an ``mcp_call`` callable in kwargs; direct_oauth mode
    accepts ``credentials`` and ``send_enabled``.
    """
    from ..config import ConnectorMode

    if settings.connector_mode == ConnectorMode.MCP:
        mcp_call = kwargs.get("mcp_call")
        if mcp_call is None:
            raise ValueError("MCP connector requires an 'mcp_call' callable")
        return McpWorkspaceConnector(mcp_call)
    return DirectOAuthConnector(
        credentials=kwargs.get("credentials"),
        send_enabled=kwargs.get("send_enabled", False),
    )


__all__ = [
    "WorkspaceConnector",
    "EmailThread",
    "CalendarEvent",
    "DraftRef",
    "Provenance",
    "ConnectorError",
    "SendNotPermitted",
    "McpWorkspaceConnector",
    "DirectOAuthConnector",
    "make_connector",
]
