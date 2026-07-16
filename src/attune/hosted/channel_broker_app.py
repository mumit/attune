"""Production composition root for the private channel broker."""

from __future__ import annotations

import os

from .audit_client import AuditWriterClient
from .channel_broker import (
    ChannelReferenceHasher,
    GoogleChatLinkBroker,
    PostgresChannelBrokerRepository,
    decode_channel_reference_key,
)
from .channel_broker_service import create_app
from .cloud_sql import iam_connection
from .google_chat_provider import GoogleChatProvider
from .vault_crypto import EnvelopeCipher, GoogleKmsKeyWrapper


def _hmac_key(secret_resource: str) -> bytes:
    from google.cloud import secretmanager

    response = secretmanager.SecretManagerServiceClient().access_secret_version(
        request={"name": f"{secret_resource}/versions/latest"}
    )
    try:
        return decode_channel_reference_key(response.payload.data)
    except ValueError as exc:
        raise RuntimeError("channel reference HMAC secret is invalid") from exc


def create_production_app():
    broker = GoogleChatLinkBroker(
        PostgresChannelBrokerRepository(iam_connection),
        AuditWriterClient(os.environ["ATTUNE_AUDIT_WRITER_URL"]),
        ChannelReferenceHasher(_hmac_key(os.environ["ATTUNE_CHANNEL_HMAC_SECRET"])),
        EnvelopeCipher(GoogleKmsKeyWrapper(os.environ["ATTUNE_CONNECTOR_KMS_KEY"])),
        GoogleChatProvider(),
    )
    return create_app(
        broker,
        expected_audience=os.environ["ATTUNE_EXPECTED_AUDIENCE"],
        expected_ingress=os.environ["ATTUNE_INGRESS_SERVICE_ACCOUNT"],
        expected_control_plane=os.environ["ATTUNE_CONTROL_PLANE_SERVICE_ACCOUNT"],
    )


app = create_production_app()
