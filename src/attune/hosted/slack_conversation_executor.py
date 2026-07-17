"""Bounded, read-only hosted Slack conversation execution.

The Slack surface reuses the channel conversation implementation with its own
fixed job kind, surface, provider constants, and reply route. Workspace reads
still use the tenant's Google connector through the private secret broker.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from .google_chat_conversation_executor import (
    GoogleChatConversationExecutor,
    PostgresGoogleChatConversationWorkRepository,
)
from .repositories import ConnectionFactory

PURPOSE = "channel.slack.converse"
CAPABILITY = "assistant.conversation.read"


class SlackReplyBroker(Protocol):
    def deliver_slack_reply(self, *, destination_id: UUID, job_id: UUID) -> bool: ...


class PostgresSlackConversationWorkRepository(
    PostgresGoogleChatConversationWorkRepository
):
    def __init__(self, connection_factory: ConnectionFactory):
        super().__init__(
            connection_factory,
            job_kind=PURPOSE,
            surface="slack",
            destination_provider="slack",
            event_provider="slack",
            event_kind="slack.message",
        )


class SlackConversationExecutor(GoogleChatConversationExecutor):
    def __init__(self, work, intents, workspace, models, replies, **kwargs):
        super().__init__(
            work,
            intents,
            workspace,
            models,
            replies,
            reply_method="deliver_slack_reply",
            intent_key_prefix="attune-slack-converse-v1:",
            **kwargs,
        )
