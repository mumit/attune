"""Composition root for the locked hosted control-plane shell."""

from __future__ import annotations

import os

from .cloud_sql import iam_connection
from .control_plane_service import create_app
from .dispatch import PostgresDispatchProducerRepository
from .audit import PostgresAuditProducerRepository
from .audit_client import AuditWriterClient
from .dispatch_broker_client import DispatchBrokerClient
from .google_connection_test import GoogleWorkspaceConnectionTest
from .google_connector_revocation import GoogleConnectorRevocation
from .hosted_channel_service import HostedChannelService
from .hosted_channels import PostgresHostedChannelRepository
from .channel_setup import PostgresHostedChannelSetupRepository
from .channel_setup_service import HostedChannelSetupService
from .channel_broker_client import ChannelBrokerClient
from .identity_session import PostgresIdentitySessionRepository
from .oauth import (
    PostgresGoogleConnectorRevocationRepository,
    PostgresGoogleOAuthStartRepository,
)
from .onboarding import PostgresHostedOnboardingRepository
from .hosted_policy import PostgresHostedPolicyRepository
from .hosted_policy_service import HostedPolicyService
from .repositories import PostgresJobRepository
from .secret_broker_mutation_client import SecretBrokerMutationClient
from .customer_export import PostgresCustomerExportRequests
from .customer_export_service import CustomerExportService
from .export_download import PostgresExportDownloadAuthorizations


def create_production_app():
    enabled = os.environ.get("ATTUNE_IDENTITY_ENABLED", "false")
    if enabled not in {"true", "false"}:
        raise ValueError("ATTUNE_IDENTITY_ENABLED must be true or false")
    identity_enabled = enabled == "true"
    oauth_enabled_value = os.environ.get("ATTUNE_GOOGLE_OAUTH_ENABLED", "false")
    if oauth_enabled_value not in {"true", "false"}:
        raise ValueError("ATTUNE_GOOGLE_OAUTH_ENABLED must be true or false")
    oauth_enabled = oauth_enabled_value == "true"
    test_enabled_value = os.environ.get("ATTUNE_GOOGLE_CONNECTION_TEST_ENABLED", "false")
    if test_enabled_value not in {"true", "false"}:
        raise ValueError("ATTUNE_GOOGLE_CONNECTION_TEST_ENABLED must be true or false")
    test_enabled = test_enabled_value == "true"
    onboarding_enabled_value = os.environ.get("ATTUNE_HOSTED_ONBOARDING_ENABLED", "false")
    if onboarding_enabled_value not in {"true", "false"}:
        raise ValueError("ATTUNE_HOSTED_ONBOARDING_ENABLED must be true or false")
    onboarding_enabled = onboarding_enabled_value == "true"
    policy_enabled_value = os.environ.get("ATTUNE_HOSTED_POLICY_ENABLED", "false")
    if policy_enabled_value not in {"true", "false"}:
        raise ValueError("ATTUNE_HOSTED_POLICY_ENABLED must be true or false")
    policy_enabled = policy_enabled_value == "true"
    channels_enabled_value = os.environ.get("ATTUNE_HOSTED_CHANNELS_ENABLED", "false")
    if channels_enabled_value not in {"true", "false"}:
        raise ValueError("ATTUNE_HOSTED_CHANNELS_ENABLED must be true or false")
    channels_enabled = channels_enabled_value == "true"
    channel_setup_enabled_value = os.environ.get(
        "ATTUNE_HOSTED_CHANNEL_SETUP_ENABLED", "false"
    )
    if channel_setup_enabled_value not in {"true", "false"}:
        raise ValueError("ATTUNE_HOSTED_CHANNEL_SETUP_ENABLED must be true or false")
    channel_setup_enabled = channel_setup_enabled_value == "true"
    channel_lifecycle_enabled_value = os.environ.get(
        "ATTUNE_HOSTED_CHANNEL_LIFECYCLE_ENABLED", "false"
    )
    if channel_lifecycle_enabled_value not in {"true", "false"}:
        raise ValueError("ATTUNE_HOSTED_CHANNEL_LIFECYCLE_ENABLED must be true or false")
    channel_lifecycle_enabled = channel_lifecycle_enabled_value == "true"
    customer_exports_enabled_value = os.environ.get(
        "ATTUNE_CUSTOMER_EXPORTS_ENABLED", "false"
    )
    if customer_exports_enabled_value not in {"true", "false"}:
        raise ValueError("ATTUNE_CUSTOMER_EXPORTS_ENABLED must be true or false")
    customer_exports_enabled = customer_exports_enabled_value == "true"
    if policy_enabled and not onboarding_enabled:
        raise ValueError("hosted policy requires hosted onboarding")
    if channels_enabled and not onboarding_enabled:
        raise ValueError("hosted channels require hosted onboarding")
    if channel_setup_enabled and not channels_enabled:
        raise ValueError("hosted channel setup requires hosted channels")
    if channel_lifecycle_enabled and not channel_setup_enabled:
        raise ValueError("hosted channel lifecycle requires hosted channel setup")
    if onboarding_enabled and not identity_enabled:
        raise ValueError("hosted onboarding requires identity")
    if test_enabled and not oauth_enabled:
        raise ValueError("Google connection test requires Google Workspace OAuth")
    if customer_exports_enabled and not identity_enabled:
        raise ValueError("customer exports require identity")
    google_oauth = (
        PostgresGoogleOAuthStartRepository(iam_connection) if oauth_enabled else None
    )
    google_tests = None
    if test_enabled:
        google_tests = GoogleWorkspaceConnectionTest(
            google_oauth,  # type: ignore[arg-type]
            PostgresDispatchProducerRepository(
                iam_connection, producer_kind="control_plane"
            ),
            PostgresJobRepository(iam_connection),
            DispatchBrokerClient(
                os.environ["ATTUNE_DISPATCH_BROKER_URL"],
                os.environ["ATTUNE_DISPATCH_BROKER_AUDIENCE"],
            ),
        )
    google_revocations = None
    if oauth_enabled:
        google_revocations = GoogleConnectorRevocation(
            PostgresGoogleConnectorRevocationRepository(iam_connection),
            SecretBrokerMutationClient(
                os.environ["ATTUNE_SECRET_BROKER_URL"],
                os.environ["ATTUNE_SECRET_BROKER_AUDIENCE"],
            ),
        )
    audit = (
        PostgresAuditProducerRepository(iam_connection, producer_kind="control_plane")
        if policy_enabled or channels_enabled or channel_setup_enabled
        else None
    )
    audit_writer = (
        AuditWriterClient(os.environ["ATTUNE_AUDIT_WRITER_URL"])
        if policy_enabled or channels_enabled or channel_setup_enabled
        else None
    )
    return create_app(
        os.environ["ATTUNE_PUBLIC_HOST"],
        identity_enabled=identity_enabled,
        project_id=os.environ.get("ATTUNE_IDENTITY_PROJECT")
        if identity_enabled
        else None,
        identity_api_key=os.environ.get("ATTUNE_IDENTITY_API_KEY")
        if identity_enabled
        else None,
        identity_auth_domain=os.environ.get("ATTUNE_IDENTITY_AUTH_DOMAIN")
        if identity_enabled
        else None,
        sessions=(
            PostgresIdentitySessionRepository(iam_connection)
            if identity_enabled
            else None
        ),
        google_oauth_enabled=oauth_enabled,
        google_oauth_client_id=(
            os.environ.get("ATTUNE_GOOGLE_OAUTH_CLIENT_ID") if oauth_enabled else None
        ),
        google_oauth_starts=google_oauth,
        google_connection_test_enabled=test_enabled,
        google_connection_tests=google_tests,
        google_connector_revocation_enabled=oauth_enabled,
        google_connector_revocations=google_revocations,
        hosted_onboarding_enabled=onboarding_enabled,
        hosted_onboarding=(
            PostgresHostedOnboardingRepository(iam_connection)
            if onboarding_enabled
            else None
        ),
        hosted_policy_enabled=policy_enabled,
        hosted_policy=(
            HostedPolicyService(
                PostgresHostedPolicyRepository(iam_connection),
                audit,
                audit_writer,
            )
            if policy_enabled
            else None
        ),
        hosted_channels_enabled=channels_enabled,
        hosted_channels=(
            HostedChannelService(
                PostgresHostedChannelRepository(iam_connection), audit, audit_writer
            )
            if channels_enabled
            else None
        ),
        hosted_channel_setup_enabled=channel_setup_enabled,
        hosted_channel_lifecycle_enabled=channel_lifecycle_enabled,
        hosted_channel_setup=(
            HostedChannelSetupService(
                PostgresHostedChannelSetupRepository(iam_connection),
                audit,
                audit_writer,
                ChannelBrokerClient(
                    os.environ["ATTUNE_CHANNEL_BROKER_URL"],
                    os.environ["ATTUNE_CHANNEL_BROKER_AUDIENCE"],
                ),
            )
            if channel_setup_enabled
            else None
        ),
        customer_exports_enabled=customer_exports_enabled,
        customer_exports=(
            CustomerExportService(
                PostgresCustomerExportRequests(iam_connection),
                PostgresDispatchProducerRepository(
                    iam_connection, producer_kind="control_plane"
                ),
                DispatchBrokerClient(
                    os.environ["ATTUNE_DISPATCH_BROKER_URL"],
                    os.environ["ATTUNE_DISPATCH_BROKER_AUDIENCE"],
                ),
                PostgresExportDownloadAuthorizations(iam_connection),
            )
            if customer_exports_enabled
            else None
        ),
    )


app = create_production_app()
