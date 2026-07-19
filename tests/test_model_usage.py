"""Offline validation for the model usage metering repositories. The
Postgres-backed behavior itself (accumulate-upsert math, RLS isolation, the
concurrent-writer race) is exercised in the gated ``test_hosted_db.py``
suite, mirroring how ``PostgresHostedChannelRepository`` has no offline test
of its own -- only its inputs are validated before ever opening a
connection."""

from __future__ import annotations

from uuid import UUID

import pytest

from attune.hosted.model_usage import (
    USAGE_WINDOW_DAYS,
    PostgresModelUsageMeterRepository,
    PostgresModelUsageQueryRepository,
)
from attune.hosted.tenant import TenantContext

TENANT = TenantContext(UUID("10000000-0000-4000-8000-000000000001"))


def _forbidden_connection():
    raise AssertionError("invalid input must not reach the database")


def test_accumulate_rejects_an_unknown_task_before_connecting():
    meter = PostgresModelUsageMeterRepository(_forbidden_connection)
    with pytest.raises(ValueError, match="task"):
        meter.accumulate(
            TENANT, task="summarize", profile="standard", success=True,
            input_tokens=1, output_tokens=1,
        )


def test_accumulate_rejects_an_out_of_vocabulary_profile_before_connecting():
    meter = PostgresModelUsageMeterRepository(_forbidden_connection)
    with pytest.raises(ValueError, match="profile"):
        meter.accumulate(
            TENANT, task="classify", profile="enterprise", success=True,
            input_tokens=1, output_tokens=1,
        )


@pytest.mark.parametrize("input_tokens,output_tokens", [(-1, 0), (0, -1)])
def test_accumulate_rejects_negative_token_counts_before_connecting(
    input_tokens, output_tokens
):
    meter = PostgresModelUsageMeterRepository(_forbidden_connection)
    with pytest.raises(ValueError, match="non-negative"):
        meter.accumulate(
            TENANT, task="classify", profile="standard", success=True,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )


def test_accumulate_rejects_a_non_bool_success_before_connecting():
    meter = PostgresModelUsageMeterRepository(_forbidden_connection)
    with pytest.raises(ValueError, match="bool"):
        meter.accumulate(
            TENANT, task="classify", profile="standard", success="yes",
            input_tokens=1, output_tokens=1,
        )


def test_usage_window_is_fixed_at_30_days():
    assert USAGE_WINDOW_DAYS == 30


def test_query_repository_is_constructible_without_touching_the_database():
    # Construction alone must never open a connection.
    PostgresModelUsageQueryRepository(_forbidden_connection)
