"""Production composition root for the hosted dispatch broker."""

from __future__ import annotations

import os

from .audit import PostgresDispatchAuditRepository
from .audit_client import AuditWriterClient
from .cloud_sql import iam_connection
from .dispatch import PostgresDispatchBrokerRepository
from .dispatch_audit import DispatchBrokerAudit
from .dispatch_broker import DispatchBroker
from .dispatch_broker_service import create_app
from .dispatch_routes import parse_routes
from .task_creator import GoogleCloudTaskCreator


def create_production_app():
    writer = AuditWriterClient(os.environ["ATTUNE_AUDIT_WRITER_URL"])
    audit = DispatchBrokerAudit(
        PostgresDispatchAuditRepository(iam_connection),
        writer,
    )
    broker = DispatchBroker(
        intents=PostgresDispatchBrokerRepository(iam_connection),
        tasks=GoogleCloudTaskCreator(
            os.environ["ATTUNE_TASK_DISPATCH_SERVICE_ACCOUNT"]
        ),
        audit=audit,
        routes=parse_routes(os.environ["ATTUNE_DISPATCH_ROUTES"]),
    )
    ingress_accounts: tuple[str, ...] = (os.environ["ATTUNE_INGRESS_SERVICE_ACCOUNT"],)
    slack_ingress_account = os.environ.get("ATTUNE_SLACK_INGRESS_SERVICE_ACCOUNT")
    if slack_ingress_account:
        ingress_accounts = ingress_accounts + (slack_ingress_account,)
    return create_app(
        broker,
        expected_audience=os.environ["ATTUNE_EXPECTED_AUDIENCE"],
        expected_callers={
            "control_plane": os.environ["ATTUNE_CONTROL_PLANE_SERVICE_ACCOUNT"],
            "ingress": ingress_accounts,
            "worker": os.environ["ATTUNE_WORKER_SERVICE_ACCOUNT"],
        },
    )


app = create_production_app()
