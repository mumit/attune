"""Content-free per-tenant model usage metering (docs/future-state.md Phase 6
"hosted operations"; hosted review gap #1 -- no billing/usage metering
existed). Two repositories over the same ``attune.model_usage_daily`` table
(migration 0047):

- ``PostgresModelUsageMeterRepository`` is the WORKER-side seam: it calls
  the SECURITY DEFINER ``attune.accumulate_model_usage`` function, the only
  mutation path (see 0047's own comment for why the worker holds no direct
  UPDATE grant). A failure here must never break the model call it is
  metering -- callers are expected to catch and log, exactly like every
  other dual-write in this codebase; this module itself does not swallow
  exceptions, since the caller (the conversation executor) is the one that
  knows the model call already succeeded and must proceed regardless.
- ``PostgresModelUsageQueryRepository`` is the CONTROL-PLANE-side seam:
  ``GET /v1/usage``'s bounded, content-free read of the tenant's own last 30
  days of aggregates, under the ordinary control-plane role's plain SELECT
  grant (read-only, no function needed).
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import date

from .model_gateway import PROFILES
from .model_gateway import TASKS as MODEL_TASKS
from .repositories import ConnectionFactory
from .tenant import TenantContext, tenant_transaction

USAGE_WINDOW_DAYS = 30


@dataclass(frozen=True)
class DailyModelUsage:
    usage_date: date
    task: str
    profile: str
    request_count: int
    input_tokens: int
    output_tokens: int
    failure_count: int


class PostgresModelUsageMeterRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def accumulate(
        self,
        context: TenantContext,
        *,
        task: str,
        profile: str,
        success: bool,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        if task not in MODEL_TASKS:
            raise ValueError("model usage task is invalid")
        if profile not in PROFILES:
            raise ValueError("model usage profile is invalid")
        if not isinstance(success, bool):
            raise ValueError("model usage outcome must be a bool")
        for count in (input_tokens, output_tokens):
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError("model usage token counts must be non-negative ints")
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    "SELECT * FROM attune.accumulate_model_usage(%s, %s, %s, %s, %s)",
                    (task, profile, success, input_tokens, output_tokens),
                )
                if cursor.fetchone() is None:
                    raise RuntimeError("model usage accumulation returned no state")


class PostgresModelUsageQueryRepository:
    def __init__(self, connection_factory: ConnectionFactory):
        self._connect = connection_factory

    def recent(self, context: TenantContext) -> list[DailyModelUsage]:
        """The tenant's own daily aggregates for the fixed, bounded
        ``USAGE_WINDOW_DAYS``-day window -- content-free by construction
        (the underlying table stores nothing but counters)."""
        with closing(self._connect()) as connection:
            with tenant_transaction(connection, context) as cursor:
                cursor.execute(
                    """
                    SELECT usage_date, task, profile, request_count,
                           input_tokens, output_tokens, failure_count
                      FROM attune.model_usage_daily
                     WHERE tenant_id = %s
                       AND usage_date >= (clock_timestamp() AT TIME ZONE 'UTC')::date
                           - %s
                     ORDER BY usage_date DESC, task, profile
                    """,
                    (context.tenant_id, USAGE_WINDOW_DAYS),
                )
                return [DailyModelUsage(*row) for row in cursor.fetchall()]
