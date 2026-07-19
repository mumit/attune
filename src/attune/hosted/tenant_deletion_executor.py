"""Owner-initiated tenant deletion (right-to-be-forgotten) executor.

Claims at most one due deletion request (grace elapsed), walks every
ERASE/CRYPTO_ERASE-classified relation in
``attune.hosted.data_lifecycle.RELATIONAL_ASSETS`` -- never a hand-copied
table list -- through the bounded, content-free ``erase_tenant_deletion_relation``
function (migration 0046), then marks the request complete or, on ambiguity,
failed. See docs/data-lifecycle.md's "Content retention and tenant deletion
design" section for the full ceremony and its reconciliation-style posture.

Resumability: a crash between calls leaves the request in ``claimed`` state
with its original ``claim_run_id``; the next invocation's
``claim_tenant_deletion`` call finds that same row and resumes with the same
run id, so every per-relation erase call (already itself idempotent -- a
table with zero remaining rows for the tenant simply returns zero) can safely
repeat from the top of the registry.

Cross-relation foreign keys are not hand-ordered here: a relation whose erase
call fails with a foreign-key violation (because a dependent row elsewhere
has not been erased yet) is deferred to a later pass, and passes repeat until
every relation is drained or no pass makes progress, at which point the
request is marked ``failed`` with a fixed, content-free reason -- an honest
stop signal, not a silent skip or a blind retry, mirroring
docs/reconciliation.md's posture for ambiguous effects.
"""

from __future__ import annotations

import json
import os
from contextlib import closing
from typing import Protocol
from uuid import UUID, uuid4

from . import data_lifecycle
from .cloud_sql import iam_connection
from .data_lifecycle import DataClass, DeletionRule
from .tenant import TenantContext

_FK_VIOLATION_SQLSTATE = "23503"
_ANCHOR_RELATIONS = ("tenants", "principals")


class ConnectorRevocation(Protocol):
    def disconnect(self, context: TenantContext, *, principal_id: UUID) -> None: ...


def _sqlstate(exc: BaseException) -> str | None:
    """Extract a PostgreSQL SQLSTATE from either psycopg or pg8000 errors."""

    value = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if value:
        return value
    args = getattr(exc, "args", None)
    if args and isinstance(args[0], dict):
        return args[0].get("C")
    return None


def erasable_relations_in_order() -> tuple[str, ...]:
    """Every relation the tenant-deletion walk must touch, registry-derived.

    Reads ``RELATIONAL_ASSETS`` directly on every call (not a module-level
    constant copied at import time) so a test can monkeypatch the registry
    and observe this function -- and therefore the walk -- pick the change up
    immediately. A DataClass/DeletionRule combination this function does not
    recognize raises rather than silently omitting the relation.
    """

    erase_rules = {DeletionRule.ERASE, DeletionRule.CRYPTO_ERASE}
    retained = {
        DataClass.DELETION_LEDGER: {DeletionRule.RETAIN_TOMBSTONE},
        DataClass.SECURITY_AUDIT: {DeletionRule.DEIDENTIFY},
    }
    tables: list[str] = []
    for asset in data_lifecycle.RELATIONAL_ASSETS:
        allowed_retained_rules = retained.get(asset.data_class)
        if allowed_retained_rules is not None:
            if asset.deletion_rule not in allowed_retained_rules:
                raise RuntimeError(
                    "tenant deletion walk cannot classify relation "
                    f"{asset.table!r} ({asset.data_class}/{asset.deletion_rule})"
                )
            continue
        if asset.deletion_rule in erase_rules:
            tables.append(asset.table)
            continue
        raise RuntimeError(
            "tenant deletion walk cannot classify relation "
            f"{asset.table!r} ({asset.data_class}/{asset.deletion_rule})"
        )
    # `tenants`/`principals` reach their terminal status only after every
    # other relation is drained (see erase_tenant_deletion_relation's
    # comment) -- an ordering choice, not a change to which relations erase.
    anchors = [table for table in tables if table in _ANCHOR_RELATIONS]
    rest = [table for table in tables if table not in _ANCHOR_RELATIONS]
    return tuple(rest + anchors)


def _fail(connection, claim_run_id: UUID, tenant_id: UUID, failure_code: str) -> None:
    try:
        with closing(connection.cursor()) as cursor:
            cursor.execute(
                "SELECT attune.fail_tenant_deletion(%s, %s, %s, %s)",
                (claim_run_id, uuid4(), tenant_id, failure_code),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def run_tenant_deletion_once(
    *,
    batch_size: int = 500,
    max_batches_per_relation: int = 4,
    connector_revocations: ConnectorRevocation | None = None,
) -> dict | None:
    if not isinstance(batch_size, int) or isinstance(batch_size, bool):
        raise TypeError("batch_size must be an integer")
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    if not isinstance(max_batches_per_relation, int) or isinstance(
        max_batches_per_relation, bool
    ):
        raise TypeError("max_batches_per_relation must be an integer")
    if not 1 <= max_batches_per_relation <= 1000:
        raise ValueError("max_batches_per_relation must be between 1 and 1000")

    relations = erasable_relations_in_order()

    with closing(iam_connection()) as connection:
        run_id = uuid4()
        try:
            with closing(connection.cursor()) as cursor:
                cursor.execute(
                    "SELECT * FROM attune.claim_tenant_deletion(%s)", (run_id,)
                )
                claim = cursor.fetchone()
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        if claim is None:
            return None
        tenant_id, deletion_request_id, requested_by, claim_run_id, resumed = claim

        if connector_revocations is not None:
            try:
                connector_revocations.disconnect(
                    TenantContext(tenant_id), principal_id=requested_by
                )
            except Exception:
                # Best-effort upstream revocation only. The generic erase
                # pass below still cryptographically erases the credential
                # row (destroys the wrapped key and ciphertext) regardless of
                # whether the upstream provider call succeeded.
                pass

        totals: dict[str, int] = {}
        pending = list(relations)
        max_passes = len(relations) + 1
        for _pass in range(max_passes):
            if not pending:
                break
            next_pending: list[str] = []
            made_progress = False
            for table in pending:
                try:
                    table_total = 0
                    with closing(connection.cursor()) as cursor:
                        for _batch in range(max_batches_per_relation):
                            cursor.execute(
                                "SELECT attune.erase_tenant_deletion_relation("
                                "%s, %s, %s, %s, %s)",
                                (claim_run_id, uuid4(), tenant_id, table, batch_size),
                            )
                            deleted = int(cursor.fetchone()[0])
                            table_total += deleted
                            if deleted < batch_size:
                                break
                    connection.commit()
                except BaseException as exc:
                    connection.rollback()
                    if _sqlstate(exc) == _FK_VIOLATION_SQLSTATE:
                        next_pending.append(table)
                        continue
                    _fail(connection, claim_run_id, tenant_id, "executor_ambiguous")
                    raise
                totals[table] = totals.get(table, 0) + table_total
                made_progress = True
            pending = next_pending
            if pending and not made_progress:
                _fail(connection, claim_run_id, tenant_id, "executor_ambiguous")
                raise RuntimeError(
                    f"tenant deletion made no progress on: {pending}"
                )
        if pending:
            _fail(connection, claim_run_id, tenant_id, "executor_ambiguous")
            raise RuntimeError(
                f"tenant deletion exceeded its pass budget with: {pending}"
            )

        try:
            with closing(connection.cursor()) as cursor:
                cursor.execute(
                    "SELECT attune.complete_tenant_deletion(%s, %s, %s)",
                    (claim_run_id, uuid4(), tenant_id),
                )
                status = cursor.fetchone()[0]
            connection.commit()
        except BaseException:
            connection.rollback()
            try:
                _fail(
                    connection, claim_run_id, tenant_id, "completion_unconfirmed"
                )
            except Exception:
                pass
            raise

    return {
        "tenant_id": str(tenant_id),
        "deletion_request_id": str(deletion_request_id),
        "resumed": bool(resumed),
        "status": status,
        "relations": totals,
    }


def main() -> None:
    enabled = os.environ.get("ATTUNE_HOSTED_DELETION_ENABLED", "false")
    if enabled not in {"true", "false"}:
        raise ValueError("ATTUNE_HOSTED_DELETION_ENABLED must be true or false")
    if enabled != "true":
        print(
            json.dumps(
                {
                    "severity": "INFO",
                    "message": "Attune tenant deletion is disabled",
                    "event": "attune_tenant_deletion_disabled",
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return
    raw_batch_size = os.environ.get("ATTUNE_DELETION_BATCH_SIZE", "500")
    raw_max_batches = os.environ.get("ATTUNE_DELETION_MAX_BATCHES_PER_RELATION", "4")
    raw_max_tenants = os.environ.get("ATTUNE_DELETION_MAX_TENANTS_PER_RUN", "5")
    try:
        batch_size = int(raw_batch_size)
        max_batches = int(raw_max_batches)
        max_tenants = int(raw_max_tenants)
    except ValueError as exc:
        raise ValueError(
            "ATTUNE_DELETION_BATCH_SIZE, ATTUNE_DELETION_MAX_BATCHES_PER_RELATION, "
            "and ATTUNE_DELETION_MAX_TENANTS_PER_RUN must be integers"
        ) from exc
    if not 1 <= max_tenants <= 100:
        raise ValueError(
            "ATTUNE_DELETION_MAX_TENANTS_PER_RUN must be between 1 and 100"
        )

    processed = []
    for _ in range(max_tenants):
        result = run_tenant_deletion_once(
            batch_size=batch_size, max_batches_per_relation=max_batches
        )
        if result is None:
            break
        processed.append(result)
    print(
        json.dumps(
            {
                "severity": "INFO",
                "message": "Attune tenant deletion completed",
                "event": "attune_tenant_deletion",
                "processed": len(processed),
                "tenants": [item["tenant_id"] for item in processed],
            },
            sort_keys=True,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
