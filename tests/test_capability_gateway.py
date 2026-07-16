from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from attune.hosted.capability_gateway import (
    CapabilityAuthority,
    CapabilityDefinition,
    CapabilityDenied,
    CapabilityRegistry,
    EmptyArguments,
    RiskTier,
    TypedCapabilityGateway,
)
from attune.hosted.tenant import TenantContext

TENANT = TenantContext(UUID("10000000-0000-4000-8000-000000000001"))
PRINCIPAL = UUID("10000000-0000-4000-8000-000000000002")
CONNECTOR = UUID("10000000-0000-4000-8000-000000000003")
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
)


def definition(**overrides):
    values = {
        "name": "google.workspace.connection.verify",
        "version": 1,
        "risk": RiskTier.R0,
        "maximum_product_risk": RiskTier.R0,
        "domain": "private_workspace",
        "provider": "google",
        "required_scopes": SCOPES,
        "arguments": EmptyArguments(),
    }
    values.update(overrides)
    return CapabilityDefinition(**values)


class Authority:
    def __init__(self, result=None, error=None):
        self.result = result or CapabilityAuthority(CONNECTOR, 7, RiskTier.R0)
        self.error = error
        self.calls = []

    def resolve(self, context, **kwargs):
        self.calls.append((context, kwargs))
        if self.error:
            raise self.error
        return self.result


def gateway(*, authority=None, capability=None):
    authority = authority or Authority()
    gateway = TypedCapabilityGateway(
        registry=CapabilityRegistry((capability or definition(),)),
        authority=authority,
    )
    return gateway, authority


def proposal(**overrides):
    value = {
        "version": 1,
        "capability": "google.workspace.connection.verify",
        "arguments": {},
    }
    value.update(overrides)
    return value


class EchoArguments:
    def reconstruct(self, value):
        return value


class BrokenArguments:
    def reconstruct(self, value):
        raise RuntimeError("schema bug with untrusted details")


class NonJsonArguments:
    def reconstruct(self, value):
        return {"value": object()}


def test_gateway_derives_authority_and_returns_immutable_trusted_request():
    subject, authority = gateway()
    admitted = subject.authorize(TENANT, principal_id=PRINCIPAL, proposal=proposal())
    assert admitted.context == TENANT
    assert admitted.principal_id == PRINCIPAL
    assert admitted.connector_id == CONNECTOR
    assert admitted.capability == "google.workspace.connection.verify"
    assert admitted.contract_version == 1
    assert admitted.risk is RiskTier.R0
    assert admitted.policy_version == 7
    assert dict(admitted.arguments) == {}
    assert authority.calls == [
        (
            TENANT,
            {"principal_id": PRINCIPAL, "definition": definition()},
        )
    ]
    with pytest.raises(TypeError):
        admitted.arguments["url"] = "https://attacker.example"  # type: ignore[index]


def test_reconstructed_arguments_are_deep_copied_and_immutable():
    typed = definition(arguments=EchoArguments())
    subject, _ = gateway(capability=typed)
    untrusted = proposal(arguments={"query": {"labels": ["inbox"]}})
    admitted = subject.authorize(TENANT, principal_id=PRINCIPAL, proposal=untrusted)
    untrusted["arguments"]["query"]["labels"].append("attacker")
    assert admitted.arguments["query"]["labels"] == ("inbox",)
    with pytest.raises(TypeError):
        admitted.arguments["query"]["extra"] = True


def test_broken_or_non_json_argument_reconstruction_fails_closed():
    for arguments in (BrokenArguments(), NonJsonArguments()):
        typed = definition(arguments=arguments)
        subject, _ = gateway(capability=typed)
        with pytest.raises(CapabilityDenied) as denied:
            subject.authorize(
                TENANT,
                principal_id=PRINCIPAL,
                proposal=proposal(),
            )
        assert denied.value.code == "arguments_invalid"
        assert "untrusted details" not in str(denied.value)


@pytest.mark.parametrize(
    "untrusted",
    [
        None,
        {},
        {"version": 1, "capability": "google.workspace.connection.verify"},
        {**proposal(), "tenant_id": str(TENANT.tenant_id)},
        {**proposal(), "connector_id": str(CONNECTOR)},
        {**proposal(), "risk": 0},
        proposal(version=True),
        proposal(version=2),
        proposal(capability="Google.Workspace.Verify"),
        proposal(arguments=[]),
        proposal(arguments={"url": "https://attacker.example"}),
        proposal(arguments={"raw_request": {"method": "DELETE"}}),
        proposal(arguments={"padding": "x" * 16_384}),
    ],
)
def test_untrusted_proposals_cannot_add_or_smuggle_authority(untrusted):
    subject, authority = gateway()
    with pytest.raises(CapabilityDenied) as denied:
        subject.authorize(TENANT, principal_id=PRINCIPAL, proposal=untrusted)
    assert denied.value.code in {"proposal_invalid", "arguments_invalid"}
    assert authority.calls == []


def test_unknown_or_wrong_contract_version_is_not_available():
    subject, authority = gateway()
    with pytest.raises(CapabilityDenied, match="capability_unavailable"):
        subject.authorize(
            TENANT,
            principal_id=PRINCIPAL,
            proposal=proposal(capability="google.gmail.messages.delete"),
        )
    assert authority.calls == []


def test_missing_or_failed_authority_is_indistinguishable_and_fails_closed():
    for authority in (Authority(result=None), Authority(error=RuntimeError("database"))):
        authority.result = None
        subject, _ = gateway(authority=authority)
        with pytest.raises(CapabilityDenied) as denied:
            subject.authorize(TENANT, principal_id=PRINCIPAL, proposal=proposal())
        assert denied.value.code == "authority_unavailable"


def test_policy_ceiling_blocks_capability_risk():
    risky = definition(
        risk=RiskTier.R1,
        maximum_product_risk=RiskTier.R1,
    )
    subject, _ = gateway(
        capability=risky,
        authority=Authority(CapabilityAuthority(CONNECTOR, 7, RiskTier.R0)),
    )
    with pytest.raises(CapabilityDenied, match="risk_exceeds_policy"):
        subject.authorize(TENANT, principal_id=PRINCIPAL, proposal=proposal())


def test_registry_and_definitions_reject_unsafe_configuration():
    with pytest.raises(ValueError, match="unique"):
        CapabilityRegistry((definition(), definition()))
    with pytest.raises(ValueError, match="product risk"):
        replace(
            definition(),
            risk=RiskTier.R2,
            maximum_product_risk=RiskTier.R1,
        )
    with pytest.raises(ValueError, match="scopes"):
        replace(definition(), required_scopes=(SCOPES[0], SCOPES[0]))
    with pytest.raises(ValueError, match="capability name"):
        replace(definition(), name="shell")


def test_verified_context_types_are_not_model_coercions():
    subject, authority = gateway()
    with pytest.raises(TypeError):
        subject.authorize(
            str(TENANT.tenant_id), principal_id=PRINCIPAL, proposal=proposal()
        )  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        subject.authorize(TENANT, principal_id=str(PRINCIPAL), proposal=proposal())  # type: ignore[arg-type]
    assert authority.calls == []
