"""Production composition root for the first deterministic hosted worker."""

from __future__ import annotations

import os

from .audit import PostgresAuditProducerRepository
from .audit_client import AuditWriterClient
from .cloud_sql import iam_connection
from .repositories import PostgresJobRepository
from .reconciliation import PostgresJobReconciliationRepository
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
    dispatcher = WorkerDispatcher(
        jobs=PostgresJobRepository(iam_connection),
        audit=audit,
        reconciliations=PostgresJobReconciliationRepository(iam_connection),
        routes=registered_routes(),
        expected_audience=os.environ["ATTUNE_EXPECTED_AUDIENCE"],
        expected_service_account=os.environ[
            "ATTUNE_TASK_DISPATCH_SERVICE_ACCOUNT"
        ],
    )
    return create_app(dispatcher)


app = create_production_app()
