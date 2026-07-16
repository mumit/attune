"""Composition root for the locked hosted control-plane shell."""

from __future__ import annotations

import os

from .cloud_sql import iam_connection
from .control_plane_service import create_app
from .dispatch import PostgresDispatchProducerRepository
from .dispatch_broker_client import DispatchBrokerClient
from .google_connection_test import GoogleWorkspaceConnectionTest
from .identity_session import PostgresIdentitySessionRepository
from .oauth import PostgresGoogleOAuthStartRepository
from .repositories import PostgresJobRepository


def create_production_app():
    enabled = os.environ.get("ATTUNE_IDENTITY_ENABLED", "false")
    if enabled not in {"true", "false"}:
        raise ValueError("ATTUNE_IDENTITY_ENABLED must be true or false")
    identity_enabled = enabled == "true"
    oauth_enabled_value = os.environ.get("ATTUNE_GOOGLE_OAUTH_ENABLED", "false")
    if oauth_enabled_value not in {"true", "false"}:
        raise ValueError("ATTUNE_GOOGLE_OAUTH_ENABLED must be true or false")
    oauth_enabled = oauth_enabled_value == "true"
    test_enabled_value = os.environ.get(
        "ATTUNE_GOOGLE_CONNECTION_TEST_ENABLED", "false"
    )
    if test_enabled_value not in {"true", "false"}:
        raise ValueError("ATTUNE_GOOGLE_CONNECTION_TEST_ENABLED must be true or false")
    test_enabled = test_enabled_value == "true"
    if test_enabled and not oauth_enabled:
        raise ValueError("Google connection test requires Google Workspace OAuth")
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
    )


app = create_production_app()
