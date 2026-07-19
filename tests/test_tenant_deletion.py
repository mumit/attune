from __future__ import annotations

import pytest

from attune.hosted.tenant_deletion import PostgresTenantDeletionRequests
from attune.hosted.tenant import TenantContext
from uuid import UUID

TENANT = TenantContext(UUID("10000000-0000-4000-8000-000000000001"))


def test_request_rejects_non_uuid_arguments():
    repo = PostgresTenantDeletionRequests(lambda: (_ for _ in ()).throw(AssertionError))
    with pytest.raises(TypeError):
        repo.request(TENANT, principal_id="not-a-uuid", session_id=UUID(int=1))
    with pytest.raises(TypeError):
        repo.request(TENANT, principal_id=UUID(int=1), session_id="not-a-uuid")


def test_cancel_rejects_non_uuid_arguments():
    repo = PostgresTenantDeletionRequests(lambda: (_ for _ in ()).throw(AssertionError))
    with pytest.raises(TypeError):
        repo.cancel(TENANT, principal_id="not-a-uuid", session_id=UUID(int=1))
