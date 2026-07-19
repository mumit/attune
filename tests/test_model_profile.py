"""Offline validation for the tenant model profile repository. The
Postgres-backed read/write behavior itself is exercised in the gated
``test_hosted_db.py`` suite (preference roundtrip, RLS isolation)."""

from __future__ import annotations

from uuid import UUID

import pytest

from attune.hosted.model_gateway import STANDARD_PROFILE
from attune.hosted.model_profile import PostgresTenantModelProfileRepository
from attune.hosted.tenant import TenantContext

TENANT = TenantContext(UUID("10000000-0000-4000-8000-000000000001"))
PRINCIPAL = UUID("10000000-0000-4000-8000-000000000002")
SESSION = UUID("10000000-0000-4000-8000-000000000003")


def _forbidden_connection():
    raise AssertionError("invalid input must not reach the database")


def test_set_rejects_a_non_uuid_principal_or_session_before_connecting():
    repository = PostgresTenantModelProfileRepository(_forbidden_connection)
    with pytest.raises(TypeError):
        repository.set(
            TENANT, principal_id="not-a-uuid", session_id=SESSION, profile="standard",
        )
    with pytest.raises(TypeError):
        repository.set(
            TENANT, principal_id=PRINCIPAL, session_id="not-a-uuid", profile="standard",
        )


@pytest.mark.parametrize("profile", ["enterprise", "", 123, None, "Standard"])
def test_set_rejects_an_out_of_vocabulary_profile_before_connecting(profile):
    repository = PostgresTenantModelProfileRepository(_forbidden_connection)
    with pytest.raises(ValueError):
        repository.set(
            TENANT, principal_id=PRINCIPAL, session_id=SESSION, profile=profile,
        )


def test_standard_profile_constant_matches_the_fixed_default():
    assert STANDARD_PROFILE == "standard"
