"""Shared plumbing behind the near-mirrored Google Chat and Slack channel
brokers (:mod:`channel_broker`, :mod:`slack_channel_broker`) -- both follow
the identical claim-then-audit-then-consume shape over a Postgres-backed
repository. What genuinely differs per provider stays in each provider's own
module (Slack encrypts a route AND a bot token where Google Chat encrypts
one route secret; Slack's top-level entry is a full OAuth code exchange
where Google Chat's is a link-code redemption; Slack alone has a message-
acknowledgment flow) -- only the mechanical, byte-identical parts move here,
following the `hosted/intelligence.py` pattern of one shared shape behind
per-plane/per-provider specifics.
"""

from __future__ import annotations

from contextlib import closing
from typing import Callable, Protocol, TypeVar
from uuid import UUID

from .repositories import ConnectionFactory

T = TypeVar("T")


class AuditWriter(Protocol):
    def write(self, audit_intent_id: UUID) -> bool: ...


def execute_claim_call(
    connection_factory: ConnectionFactory, statement: str, values: tuple
) -> tuple:
    """Run one claim/consume/complete SQL function call in its own
    transaction: commit on a returned row, roll back on any error --
    including "no row", which is always a violated repository contract,
    never a legitimate empty result for these single-row functions."""
    with closing(connection_factory()) as connection:
        try:
            with closing(connection.cursor()) as cursor:
                cursor.execute(statement, values)
                row = cursor.fetchone()
            if row is None:
                raise RuntimeError("channel broker returned no state")
            connection.commit()
            return row
        except BaseException:
            connection.rollback()
            raise


def write_or_raise(audit_writer: AuditWriter, audit_intent_id: UUID, message: str) -> None:
    """Every broker outcome (link, delivery, message accept) is worthless
    without its audit record, so a write failure is always a hard,
    fail-closed error -- never a warning."""
    if not audit_writer.write(audit_intent_id):
        raise RuntimeError(message)


def swallow_complete_failure(
    audit_writer: AuditWriter, complete_call: Callable[[], object]
) -> None:
    """Best-effort completion + audit for a delivery attempt that has
    already failed. Always called from an ``except`` block that is about to
    re-raise the original error, so any secondary failure recording that
    failure must never mask it."""
    try:
        completed = complete_call()
        audit_writer.write(completed.outcome_audit_intent_id)
    except Exception:
        pass


def deliver_and_audit(
    *,
    pre_audit_intent_id: UUID,
    audit_writer: AuditWriter,
    on_failure: Callable[[], None],
    unavailable_message: str,
    send: Callable[[], T],
) -> T:
    """The claim-already-verified core of every delivery attempt (a
    connection test, a conversation reply, a proactive brief), identical
    across Google Chat and Slack: write the pre-effect audit, failing closed
    with a compensating ``on_failure`` if it can't be written, then run the
    provider-specific ``send`` -- compensating and re-raising on ANY failure
    from either step, never just the second one."""
    if not audit_writer.write(pre_audit_intent_id):
        on_failure()
        raise RuntimeError(unavailable_message)
    try:
        return send()
    except BaseException:
        on_failure()
        raise
