"""Production composition root for the first deterministic hosted worker."""

from __future__ import annotations

import os

from .audit import PostgresAuditProducerRepository
from .audit_client import AuditWriterClient
from .cloud_sql import iam_connection
from .google_gmail_profile_executor import GoogleGmailProfileExecutor
from .google_workspace_verification_executor import (
    GoogleWorkspaceVerificationExecutor,
)
from .repositories import PostgresJobRepository
from .reconciliation import PostgresJobReconciliationRepository
from .secret_broker_client import SecretBrokerClient
from .vault import PostgresCredentialIntentRepository
from .worker_audit import WorkerAudit
from .worker_dispatch import WorkerDispatcher
from .worker_routes import registered_routes
from .worker_service import create_app


def create_production_app():
    audit = WorkerAudit(
        PostgresAuditProducerRepository(
            iam_connection,
            producer_kind="worker",
        ),
        AuditWriterClient(os.environ["ATTUNE_AUDIT_WRITER_URL"]),
    )
    google_gmail_profile = None
    enabled = os.environ.get("ATTUNE_ENABLE_GOOGLE_GMAIL_PROFILE", "false")
    if enabled not in {"true", "false"}:
        raise ValueError("ATTUNE_ENABLE_GOOGLE_GMAIL_PROFILE must be true or false")
    if enabled == "true":
        google_gmail_profile = GoogleGmailProfileExecutor(
            PostgresCredentialIntentRepository(
                iam_connection,
                producer_kind="worker",
            ),
            SecretBrokerClient(
                os.environ["ATTUNE_SECRET_BROKER_URL"],
                os.environ["ATTUNE_SECRET_BROKER_AUDIENCE"],
            ),
        )
    google_workspace_verification = None
    workspace_enabled = os.environ.get(
        "ATTUNE_ENABLE_GOOGLE_WORKSPACE_VERIFICATION", "false"
    )
    if workspace_enabled not in {"true", "false"}:
        raise ValueError(
            "ATTUNE_ENABLE_GOOGLE_WORKSPACE_VERIFICATION must be true or false"
        )
    if workspace_enabled == "true":
        google_workspace_verification = GoogleWorkspaceVerificationExecutor(
            PostgresCredentialIntentRepository(
                iam_connection,
                producer_kind="worker",
            ),
            SecretBrokerClient(
                os.environ["ATTUNE_SECRET_BROKER_URL"],
                os.environ["ATTUNE_SECRET_BROKER_AUDIENCE"],
            ),
        )
    dispatcher = WorkerDispatcher(
        jobs=PostgresJobRepository(iam_connection),
        audit=audit,
        reconciliations=PostgresJobReconciliationRepository(iam_connection),
        routes=registered_routes(
            google_gmail_profile=google_gmail_profile,
            google_workspace_verification=google_workspace_verification,
        ),
        expected_audience=os.environ["ATTUNE_EXPECTED_AUDIENCE"],
        expected_service_account=os.environ[
            "ATTUNE_TASK_DISPATCH_SERVICE_ACCOUNT"
        ],
    )
    return create_app(dispatcher)


app = create_production_app()
