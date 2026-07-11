"""Event ingestion (design doc 4.3, 4.6).

Gmail: users.watch -> Cloud Pub/Sub pointer -> users.history.list. Watch expires
every 7 days; renew daily (gmail_watch.ensure_watch). Notifications carry the
LATEST historyId, so reconciliation runs from the STORED baseline
(gmail_history.process_notification), dedupes by threadId, and handles the stale-
historyId 404 as a distinct HistoryExpired re-sync signal.

Net security goal: the box holding credentials/memory has NO open inbound port.
The Pub/Sub HTTP receipt + base64url decode happens in a thin republisher
outside this process; this code takes already-decoded notifications.

CRITICAL: downstream, every ingested payload is provenance-tagged untrusted
(handled at the connector boundary, connectors.Provenance.FETCHED) before it
reaches the model.
"""

from .gmail_watch import WatchResult, WatchState, ensure_watch
from .gmail_history import (
    HistoryExpired,
    MailboxChanges,
    decode_pubsub_message,
    process_notification,
)
from .chat_events import (
    ChatMessage,
    SubscriptionResult,
    SubscriptionState,
    ensure_subscription,
    process_chat_event,
)

__all__ = [
    "ensure_watch",
    "WatchResult",
    "WatchState",
    "process_notification",
    "decode_pubsub_message",
    "MailboxChanges",
    "HistoryExpired",
    "ChatMessage",
    "SubscriptionResult",
    "SubscriptionState",
    "ensure_subscription",
    "process_chat_event",
]
