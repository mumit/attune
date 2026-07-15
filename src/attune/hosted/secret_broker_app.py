"""Production composition root for the hosted secret broker."""

from __future__ import annotations

import os

from .audit import PostgresAuditProducerRepository
from .audit_client import AuditWriterClient
from .cloud_sql import iam_connection
from .secret_audit import SecretBrokerAudit
from .secret_broker import SecretBroker
from .secret_broker_service import create_app
from .vault import PostgresSecretBrokerRepository
from .vault_crypto import EnvelopeCipher, GoogleKmsKeyWrapper


def create_production_app():
    producer = PostgresAuditProducerRepository(
        iam_connection,
        producer_kind="secret_broker",
    )
    audit = SecretBrokerAudit(
        producer,
        AuditWriterClient(os.environ["ATTUNE_AUDIT_WRITER_URL"]),
    )
    broker = SecretBroker(
        vault=PostgresSecretBrokerRepository(iam_connection),
        cipher=EnvelopeCipher(
            GoogleKmsKeyWrapper(os.environ["ATTUNE_CONNECTOR_KMS_KEY"])
        ),
        audit=audit,
    )
    return create_app(
        broker,
        expected_audience=os.environ["ATTUNE_EXPECTED_AUDIENCE"],
        expected_control_plane=os.environ[
            "ATTUNE_CONTROL_PLANE_SERVICE_ACCOUNT"
        ],
    )


app = create_production_app()
