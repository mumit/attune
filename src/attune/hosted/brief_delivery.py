"""Hosted proactive brief delivery (``docs/future-state.md`` Phase 5 item 4;
``docs/gap-analysis.md`` G12).

Every earlier hosted surface is reactive: a channel message or a browser
request triggers exactly one bounded read-only turn. This is the first
PROACTIVE hosted job -- ``channel.brief.deliver`` -- assembled from a
producer-created job (``hosted.brief_producer``), never from an inbound
provider message.

**Reuse, not reimplementation.** Spine ranking is the exact
:func:`attune.brief.build_spine` local triage/briefs already use -- this
module imports it directly rather than duplicating the correlation/ranking
rules (``orchestrator/correlation.py``'s own module docstring names this as
the intended hosted seam: "a future hosted brief assembler calls
``correlate``/``from_attention_item`` directly ... over
``PostgresAttentionStore.recent()`` results instead of
``JsonAttentionStore`` ones"). Counterpart tiers come from stage 1's
:class:`~attune.hosted.intelligence.PostgresImportanceProfile`; attention
items come from :class:`~attune.hosted.intelligence.PostgresAttentionStore`
(empty in production today -- no executor writes to it yet -- but the seam is
wired end to end, exactly like stage 1 documented).

**The one real adapter.** The hosted secret broker returns
:class:`~attune.hosted.secret_broker_client.GmailThreadSummary`/
:class:`~attune.hosted.secret_broker_client.CalendarEventSummary` -- a
different, more data-minimized shape than the local
:class:`~attune.connectors.base.EmailThread`/:class:`~attune.connectors.base.CalendarEvent`
``build_spine``/``orchestrator/correlation.py`` are written against (no
message body ever leaves the secret broker; the hosted summary has one
``sender``/``date`` pair rather than first/last-message fields). Rather than
touching ``brief.py`` (already a pure, public seam after the ``build_spine``
rename -- no entanglement to fix), :func:`_thread_from_summary`/
:func:`_event_from_summary` below are the small, local, one-directional
adapters that let this module hand real ``EmailThread``/``CalendarEvent``
instances to the shared ranking code, same as the local product does.

**Deterministic, no model call.** Unlike the local daily brief (one
``converse`` model call to prose-summarize the untrusted block), this hosted
job renders the spine and bounded per-source sections as plain text --
smaller surface, no model-gateway dependency for a job that runs on a timer
rather than in response to a request, and content-free by construction (the
audit records counts, never text).

**Delivery reuses the channel broker exactly like a conversation reply.**
See ``docs/hosted-channels.md``'s brief-delivery section and
``sql/0044_hosted_brief_delivery.sql``'s header comment for why a brief gets
its own small ``hosted_brief_deliveries`` table (one job can legitimately fan
out to N destinations, unlike a conversation reply) rather than being folded
into ``conversations``/``conversation_turns``.

Dormant unless ``ATTUNE_ENABLE_HOSTED_BRIEF`` is set (default off, mirroring
every other Phase 5 stage) -- see ``docs/decisions.md``.
"""

from __future__ import annotations

import hashlib
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Protocol, Sequence
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..brief import build_spine
from ..connectors.base import CalendarEvent, EmailThread
from ..orchestrator.attention import AttentionItem
from .repositories import ConnectionFactory, HostedJob, _bounded_text
from .secret_broker_client import CalendarEventSummary, GmailThreadSummary
from .tenant import TenantContext, tenant_transaction
from .vault import CredentialIntent, PostgresCredentialIntentRepository

PURPOSE = "channel.brief.deliver"
CAPABILITY = "assistant.brief.deliver"
GMAIL_CAPABILITY = "google.gmail.threads.read"
CALENDAR_CAPABILITY = "google.calendar.events.read"

# Same numeric caps as the conversational brief route
# (``GoogleChatConversationExecutor._respond``'s ``route == "brief"`` branch)
# -- a proactive brief must never read more than an on-demand one does.
GMAIL_LIMIT = 10
CALENDAR_LIMIT = 25
GMAIL_QUERY = "is:unread newer_than:1d"
CALENDAR_WINDOW_HOURS = 24

MAX_BRIEF_TEXT_CHARS = 8_000
MAX_DESTINATIONS_PER_RUN = 8


@dataclass(frozen=True)
class BriefWork:
    principal_id: UUID
    connector_id: UUID


@dataclass(frozen=True)
class BriefDestination:
    id: UUID
    provider: str


class WorkspaceBroker(Protocol):
    def google_gmail_threads(
        self, intent_id: UUID, *, query: str, limit: int = 10
    ) -> Sequence[GmailThreadSummary]: ...

    def google_calendar_events(
        self, intent_id: UUID, *, time_min: datetime, time_max: datetime, limit: int = 25
    ) -> Sequence[CalendarEventSummary]: ...


class IntentRepository(Protocol):
    def request(
        self, context: TenantContext, *, connector_id: UUID, operation: str,
        capability: str, idempotency_key: bytes, expires_at: datetime,
    ) -> CredentialIntent: ...


class BriefReplyBroker(Protocol):
    def deliver_google_chat_brief(self, *, destination_id: UUID, job_id: UUID) -> bool: ...
    def deliver_slack_brief(self, *, destination_id: UUID, job_id: UUID) -> bool: ...


class BriefAuditSink(Protocol):
    """Content-free, counts-only audit (module docstring). Deliberately the
    exact shape ``worker_audit.WorkerMemoryAudit`` already implements -- the
    composition root reuses that class rather than a bespoke one."""

    def record(
        self, context: TenantContext, *, action: str, outcome: str, job_id: str, count: int,
    ) -> None: ...


class PostgresHostedBriefRepository:
    """Resolve job authority, list brief-eligible destinations, and durably
    propose one destination's rendered brief text (module docstring)."""

    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def resolve(self, context: TenantContext, job: HostedJob) -> BriefWork:
        payload = _payload(job)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT principal.id, connector.id
                      FROM attune.jobs job
                      JOIN attune.principals principal
                        ON principal.tenant_id = job.tenant_id
                       AND principal.id = (job.payload->>'principal_id')::uuid
                      JOIN attune.connectors connector
                        ON connector.tenant_id = job.tenant_id
                       AND connector.principal_id = principal.id
                       AND connector.provider = 'google'
                     WHERE job.tenant_id = %s AND job.id = %s
                       AND job.kind = %s AND job.capability = %s
                       AND job.state = 'leased'
                       AND principal.status = 'active'
                       AND connector.status = 'active'
                       AND EXISTS (
                           SELECT 1 FROM attune.policies policy
                            WHERE policy.tenant_id = job.tenant_id AND policy.active
                       )
                     LIMIT 2
                    """,
                    (context.tenant_id, job.id, PURPOSE, CAPABILITY),
                )
                rows = cursor.fetchall()
        if len(rows) != 1:
            raise RuntimeError("brief job authority is unavailable")
        work = BriefWork(*rows[0])
        if work.principal_id != payload["principal_id"]:
            raise RuntimeError("brief job payload changed")
        return work

    def list_brief_destinations(
        self, context: TenantContext, *, principal_id: UUID
    ) -> tuple[BriefDestination, ...]:
        """Every ACTIVE, verified owner-DM destination whose stored
        preference includes briefs for its own provider (hosted-channels.md)
        -- deliberately reads ``brief_channels``, never ``interaction_channels``."""
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT destination.id, destination.provider
                      FROM attune.hosted_channel_destinations destination
                      JOIN attune.hosted_channel_preferences preference
                        ON preference.tenant_id = destination.tenant_id
                       AND preference.owner_principal_id = destination.owner_principal_id
                     WHERE destination.tenant_id = %s
                       AND destination.owner_principal_id = %s
                       AND destination.visibility = 'owner_dm'
                       AND destination.status = 'active'
                       AND destination.delivery_verified_at IS NOT NULL
                       AND destination.provider = ANY(preference.brief_channels)
                     ORDER BY destination.provider, destination.id
                     LIMIT %s
                    """,
                    (context.tenant_id, principal_id, MAX_DESTINATIONS_PER_RUN),
                )
                return tuple(
                    BriefDestination(id=row[0], provider=row[1])
                    for row in cursor.fetchall()
                )

    def propose_delivery(
        self, context: TenantContext, *, job_id: UUID, destination_id: UUID, brief_text: str,
    ) -> None:
        """Durably store one destination's rendered brief text under the
        worker's own tenant-scoped write authority (module docstring --
        mirrors ``PostgresGoogleChatConversationWorkRepository.append_assistant``'s
        trust posture for its own tenant). Idempotent: a retried job reuses
        whatever text a prior attempt already stored for this destination."""
        _bounded_text("brief_text", brief_text, MAX_BRIEF_TEXT_CHARS)
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    INSERT INTO attune.hosted_brief_deliveries
                        (tenant_id, job_id, destination_id, brief_text)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (tenant_id, job_id, destination_id) DO NOTHING
                    """,
                    (context.tenant_id, job_id, destination_id, brief_text),
                )


class HostedBriefExecutor:
    """Assemble and deliver one tenant's proactive brief (module docstring).

    ``importance_profile_factory``/``attention_store_factory`` are injected
    as CALLABLES, not concrete stage-1 store instances, so this executor is
    fully offline-testable with in-memory fakes (CLAUDE.md "inject ...
    persistence paths") -- exactly the "one short-lived instance per job"
    binding ``attune.hosted.intelligence``'s own module docstring documents,
    just constructed by the composition root's factory rather than this
    class importing ``PostgresImportanceProfile``/``PostgresAttentionStore``
    directly. Each factory receives ``(context, principal_id)`` and returns
    an object matching the local ``ImportanceProfile``/``AttentionStore``
    protocol shapes (``orchestrator/importance.py``/``orchestrator/attention.py``).
    """

    def __init__(
        self,
        work: PostgresHostedBriefRepository,
        intents: PostgresCredentialIntentRepository | IntentRepository,
        workspace: WorkspaceBroker,
        replies: BriefReplyBroker,
        importance_profile_factory: Callable[[TenantContext, UUID], object],
        attention_store_factory: Callable[[TenantContext, UUID], object],
        *,
        now: Callable[[], datetime] | None = None,
        timezone_name: str = "UTC",
        audit: BriefAuditSink | None = None,
        intent_key_prefix: str = "attune-hosted-brief-v1:",
    ):
        self._work = work
        self._intents = intents
        self._workspace = workspace
        self._replies = replies
        self._importance_profile_factory = importance_profile_factory
        self._attention_store_factory = attention_store_factory
        self._now = now or (lambda: datetime.now(timezone.utc))
        if not isinstance(timezone_name, str) or not 1 <= len(timezone_name) <= 255:
            raise ValueError("hosted timezone is invalid")
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("hosted timezone is invalid") from error
        self._timezone_name = timezone_name
        self._audit = audit
        self._intent_key_prefix = intent_key_prefix

    def __call__(self, context: TenantContext, job: HostedJob) -> None:
        authority = self._work.resolve(context, job)
        current = self._now()
        if current.tzinfo is None:
            raise RuntimeError("worker clock must be timezone-aware")

        gmail_intent = self._intent(context, job, authority.connector_id, GMAIL_CAPABILITY, current)
        raw_threads = self._workspace.google_gmail_threads(
            gmail_intent, query=GMAIL_QUERY, limit=GMAIL_LIMIT
        )
        calendar_intent = self._intent(context, job, authority.connector_id, CALENDAR_CAPABILITY, current)
        raw_events = self._workspace.google_calendar_events(
            calendar_intent, time_min=current,
            time_max=current + timedelta(hours=CALENDAR_WINDOW_HOURS), limit=CALENDAR_LIMIT,
        )
        threads = [_thread_from_summary(item) for item in raw_threads]
        events = [_event_from_summary(item, now=current) for item in raw_events]

        importance_profile = self._importance_profile_factory(context, authority.principal_id)
        attention_store = self._attention_store_factory(context, authority.principal_id)
        attention_items: list[AttentionItem] = list(
            attention_store.recent(since=current - timedelta(hours=24))
        )

        spine = build_spine(
            threads, events, attention_items,
            importance_profile=importance_profile, now=current, pending=None,
        )
        text = _render_brief(spine, threads, events, timezone_name=self._timezone_name, now=current)

        destinations = self._work.list_brief_destinations(
            context, principal_id=authority.principal_id
        )
        delivered = 0
        for destination in destinations:
            self._work.propose_delivery(
                context, job_id=job.id, destination_id=destination.id, brief_text=text,
            )
            deliver = (
                self._replies.deliver_google_chat_brief
                if destination.provider == "google_chat"
                else self._replies.deliver_slack_brief
            )
            if not deliver(destination_id=destination.id, job_id=job.id):
                raise RuntimeError("brief was not delivered to a destination")
            delivered += 1

        self._audit_count(context, job, "brief.assemble", count=len(spine))
        self._audit_count(context, job, "brief.deliver", count=delivered)

    def _intent(
        self, context: TenantContext, job: HostedJob, connector_id: UUID,
        capability: str, now: datetime,
    ) -> UUID:
        key = hashlib.sha256(
            (
                self._intent_key_prefix
                + f"{capability}:{context.tenant_id}:{job.id}:{job.attempts}:{connector_id}"
            ).encode()
        ).digest()
        intent = self._intents.request(
            context, connector_id=connector_id, operation="use",
            capability=capability, idempotency_key=key,
            expires_at=now + timedelta(minutes=2),
        )
        if intent.state != "requested":
            raise RuntimeError("credential intent is unavailable")
        return intent.id

    def _audit_count(
        self, context: TenantContext, job: HostedJob, action: str, *, count: int,
    ) -> None:
        if self._audit is None:
            return
        self._audit.record(
            context, action=action, outcome="allowed", job_id=str(job.id), count=count,
        )


def _payload(job: HostedJob) -> dict[str, object]:
    if job.kind != PURPOSE or job.capability != CAPABILITY:
        raise ValueError("brief job does not match the fixed route")
    if not isinstance(job.payload, dict) or set(job.payload) != {
        "schema_version", "principal_id",
    } or job.payload.get("schema_version") != 1:
        raise ValueError("brief job payload does not match the contract")
    value = job.payload["principal_id"]
    if not isinstance(value, str):
        raise ValueError("brief job principal reference is invalid")
    try:
        principal_id = UUID(value)
    except ValueError as error:
        raise ValueError("brief job principal reference is invalid") from error
    if str(principal_id) != value:
        raise ValueError("brief job principal reference is invalid")
    return {"principal_id": principal_id}


def _bounded_line(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _parse_email_date(value: str) -> datetime | None:
    """Best-effort parse of an RFC 2822 mail ``Date`` header string, or
    ``None`` on any failure -- a malformed/foreign date must never break
    spine assembly (mirrors ``orchestrator/correlation.py``'s own
    ``from_mail_thread`` last-resort-timestamp fallback)."""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _thread_from_summary(item: GmailThreadSummary) -> EmailThread:
    """Adapt the hosted, data-minimized :class:`GmailThreadSummary` into the
    local :class:`~attune.connectors.base.EmailThread` shape
    ``brief.build_spine``/``orchestrator/correlation.py`` are written
    against (module docstring's "the one real adapter"). No message body
    ever leaves the secret broker, so ``body`` is always empty here --
    nothing downstream of this adapter reads it (the spine only reads
    ``subject``/``snippet``/``from_addr``/``last_message_at``)."""
    ts = _parse_email_date(item.date)
    return EmailThread(
        thread_id=item.thread_id,
        subject=item.subject,
        snippet=item.snippet,
        from_addr=item.sender,
        body="",
        received_at=ts,
        last_from_addr=item.sender,
        last_message_at=ts,
    )


def _event_from_summary(item: CalendarEventSummary, *, now: datetime) -> CalendarEvent:
    """Adapt :class:`CalendarEventSummary` into the local
    :class:`~attune.connectors.base.CalendarEvent` shape. ``start``/``end``
    fall back to ``now`` on an unparseable value rather than raising --
    ranking/rendering must never break on a foreign date format."""
    start = _parse_iso(item.start) or now
    end = _parse_iso(item.end) or start
    return CalendarEvent(event_id=item.event_id, summary=item.summary, start=start, end=end)


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _render_brief(
    spine: list[str], threads: list[EmailThread], events: list[CalendarEvent],
    *, timezone_name: str, now: datetime,
) -> str:
    """Deterministic, model-free brief text (module docstring): the ranked
    spine leads, followed by bounded unread-mail and today's-events sections
    -- the same sections the local brief renders into its untrusted block,
    minus the prose summary a model would otherwise add."""
    zone = ZoneInfo(timezone_name)
    lines = ["WHAT MATTERS NOW:"]
    lines.extend(spine or ["(nothing across sources needs attention right now)"])
    lines.append("")
    lines.append(f"UNREAD MAIL ({len(threads)}):")
    if threads:
        for thread in threads:
            lines.append(
                f"- {_bounded_line(thread.from_addr, 120)}: "
                f"{_bounded_line(thread.subject, 160)}"
            )
    else:
        lines.append("(none)")
    lines.append("")
    lines.append(f"UPCOMING EVENTS ({len(events)}):")
    if events:
        for event in events:
            lines.append(
                f"- {event.start.astimezone(zone):%H:%M} "
                f"{_bounded_line(event.summary, 160)}"
            )
    else:
        lines.append("(none)")
    text = "\n".join(lines)
    return text[:MAX_BRIEF_TEXT_CHARS]
