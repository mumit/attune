"""Synthetic end-to-end validation for brokered hosted task delivery."""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import UUID

from .cloud_sql import iam_connection
from .dispatch import PostgresDispatchProducerRepository
from .repositories import PostgresJobRepository
from .tenant import TenantContext, tenant_transaction

SMOKE_TENANT = UUID("00000000-0000-4000-8000-000000000001")
SMOKE_SLUG = "platform-smoke-development"


def _row_tuple(row: Iterable[object] | None) -> tuple[object, ...] | None:
    return None if row is None else tuple(row)


def _origin(value: str, name: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(f"{name} must be an HTTPS origin")
    return value.rstrip("/")


def _ensure_smoke_tenant(region: str) -> TenantContext:
    if not region or len(region) > 63:
        raise ValueError("smoke region is invalid")
    context = TenantContext(SMOKE_TENANT)
    with closing(iam_connection()) as connection:
        with tenant_transaction(connection, context) as cursor:
            cursor.execute(
                """
                INSERT INTO attune.tenants (id, slug, region)
                VALUES (%s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (SMOKE_TENANT, SMOKE_SLUG, region),
            )
            cursor.execute(
                "SELECT slug, region, status FROM attune.tenants WHERE id = %s",
                (SMOKE_TENANT,),
            )
            row = cursor.fetchone()
            if _row_tuple(row) != (SMOKE_SLUG, region, "active"):
                raise RuntimeError("platform smoke tenant does not match")
    return context


def _invoke_broker(url: str, audience: str, intent_id: UUID) -> None:
    import requests
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    token = id_token.fetch_id_token(Request(), audience)
    response = requests.post(
        f"{url}/v1/dispatch-intents/dispatch",
        json={"intent_id": str(intent_id)},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
        allow_redirects=False,
    )
    if response.status_code != 204:
        raise RuntimeError("dispatch broker did not accept the smoke intent")


def _wait_for_success(
    jobs: PostgresJobRepository,
    context: TenantContext,
    job_id: UUID,
    *,
    timeout_seconds: int = 60,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        job = jobs.get(context, job_id)
        if job is not None and job.state == "succeeded":
            return
        if job is not None and job.state in {
            "failed",
            "reconcile",
            "cancelled",
        }:
            raise RuntimeError("platform smoke job entered a terminal failure")
        time.sleep(1)
    raise RuntimeError("platform smoke job did not complete")


def _verify_worker_audit(context: TenantContext, job_id: UUID) -> None:
    target_hash = hashlib.sha256(f"attune-job-v1:{job_id}".encode()).digest()
    with closing(iam_connection()) as connection:
        with tenant_transaction(connection, context) as cursor:
            cursor.execute(
                """
                SELECT action, outcome FROM attune.audit_events
                 WHERE tenant_id = %s AND target_type = 'job'
                   AND target_ref_hash = %s
                 ORDER BY sequence
                """,
                (context.tenant_id, target_hash),
            )
            events = [tuple(row) for row in cursor.fetchall()]
    if events != [
        ("worker.job.claimed", "allowed"),
        ("worker.job.execute", "allowed"),
    ]:
        raise RuntimeError("platform smoke worker audit is incomplete")


def main() -> None:
    broker_url = _origin(os.environ["ATTUNE_DISPATCH_BROKER_URL"], "broker URL")
    audience = _origin(
        os.environ["ATTUNE_DISPATCH_BROKER_AUDIENCE"],
        "broker audience",
    )
    context = _ensure_smoke_tenant(os.environ["ATTUNE_REGION"])
    key = hashlib.sha256(os.urandom(32)).digest()
    dispatch = PostgresDispatchProducerRepository(
        iam_connection,
        producer_kind="control_plane",
    ).enqueue(
        context,
        kind="platform.smoke",
        capability="platform.smoke",
        payload={"probe": "dispatch-v1"},
        idempotency_key=key,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    _invoke_broker(broker_url, audience, dispatch.intent.id)
    _wait_for_success(PostgresJobRepository(iam_connection), context, dispatch.job.id)
    _verify_worker_audit(context, dispatch.job.id)
    print("PASS brokered dispatch round trip")


if __name__ == "__main__":
    main()
