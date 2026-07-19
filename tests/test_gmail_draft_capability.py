from __future__ import annotations

import pytest

from attune.hosted.capability_gateway import RiskTier
from attune.hosted.gmail_draft_capability import (
    DRAFT_CAPABILITY,
    DRAFT_CONTRACT_VERSION,
    DRAFT_DOMAIN,
    DRAFT_PROVIDER,
    DRAFT_REQUIRED_SCOPES,
    GmailDraftCreateArguments,
    build_draft_capability_registry,
)


def test_registry_registers_exactly_the_one_r2_capability():
    registry = build_draft_capability_registry()
    definition = registry.get(DRAFT_CAPABILITY)
    assert definition is not None
    assert definition.version == DRAFT_CONTRACT_VERSION == 1
    assert definition.risk is RiskTier.R2
    assert definition.maximum_product_risk is RiskTier.R2
    assert definition.domain == DRAFT_DOMAIN
    assert definition.provider == DRAFT_PROVIDER == "google"
    assert definition.required_scopes == DRAFT_REQUIRED_SCOPES
    assert DRAFT_REQUIRED_SCOPES == (
        "https://www.googleapis.com/auth/gmail.compose",
    )
    assert registry.get("google.gmail.draft.send") is None


def test_arguments_reconstruct_exact_bounded_shape():
    arguments = GmailDraftCreateArguments()
    reconstructed = arguments.reconstruct(
        {"thread_ref": "18b2f3a1c2d3e4f5", "body": "See you then."}
    )
    assert reconstructed == {"thread_ref": "18b2f3a1c2d3e4f5", "body": "See you then."}


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"thread_ref": "abc"},
        {"body": "hi"},
        {"thread_ref": "abc", "body": "hi", "cc": "victim@example.com"},
        {"thread_ref": "../etc/passwd", "body": "hi"},
        {"thread_ref": "https://attacker.example", "body": "hi"},
        {"thread_ref": "x" * 181, "body": "hi"},
        {"thread_ref": "", "body": "hi"},
        {"thread_ref": 12345, "body": "hi"},
        {"thread_ref": "abc", "body": ""},
        {"thread_ref": "abc", "body": "x" * 10_001},
        {"thread_ref": "abc", "body": 12345},
        {"thread_ref": "abc", "body": None},
    ],
)
def test_arguments_reject_out_of_schema_values(value):
    with pytest.raises(ValueError):
        GmailDraftCreateArguments().reconstruct(value)


def test_body_at_exact_bound_is_accepted():
    reconstructed = GmailDraftCreateArguments().reconstruct(
        {"thread_ref": "abc", "body": "x" * 10_000}
    )
    assert len(reconstructed["body"]) == 10_000
