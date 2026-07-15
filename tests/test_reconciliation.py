from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from attune.hosted.reconciliation import PostgresJobReconciliationRepository
from attune.hosted.repositories import HostedJob
from attune.hosted.tenant import TenantContext


def _job() -> HostedJob:
    now = datetime.now(timezone.utc)
    return HostedJob(
        id=UUID("10000000-0000-4000-8000-000000000801"),
        kind="calendar.write",
        state="leased",
        capability="calendar.write",
        payload={},
        attempts=1,
        available_at=now,
        lease_expires_at=now,
    )


def test_reconciliation_rejects_unregistered_reason_before_connecting():
    repository = PostgresJobReconciliationRepository(
        lambda: (_ for _ in ()).throw(AssertionError("must not connect"))
    )
    with pytest.raises(ValueError, match="reconciliation reason"):
        repository.open(
            TenantContext(UUID("10000000-0000-4000-8000-000000000802")),
            _job(),
            reason_code="model_decided",
        )


def test_reconciliation_rejects_malformed_provider_reference_before_connecting():
    repository = PostgresJobReconciliationRepository(
        lambda: (_ for _ in ()).throw(AssertionError("must not connect"))
    )
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        repository.open(
            TenantContext(UUID("10000000-0000-4000-8000-000000000802")),
            _job(),
            reason_code="executor_ambiguous",
            provider_request_ref_hash=b"provider-request-id",
        )
