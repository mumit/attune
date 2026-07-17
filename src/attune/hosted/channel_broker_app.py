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
from .slack_channel_broker import (
    PostgresSlackChannelBrokerRepository,
    SlackInstallBroker,
    SlackReferenceHasher,
)
from .slack_provider import SlackProvider
from .vault_crypto import EnvelopeCipher, GoogleKmsKeyWrapper


def _secret(secret_resource: str) -> bytes:
    from google.cloud import secretmanager

    response = secretmanager.SecretManagerServiceClient().access_secret_version(
        request={"name": f"{secret_resource}/versions/latest"}
    )
    return response.payload.data


def _hmac_key(secret_resource: str) -> bytes:
    try:
        return decode_channel_reference_key(_secret(secret_resource))
    except ValueError as exc:
        raise RuntimeError("channel reference HMAC secret is invalid") from exc


def _slack_broker(hmac_key: bytes, cipher: EnvelopeCipher, writer: AuditWriterClient):
    if os.environ.get("ATTUNE_SLACK_CHANNEL_ENABLED") != "true":
        return None
    client_secret = _secret(os.environ["ATTUNE_SLACK_CLIENT_SECRET"]).decode("utf-8").strip()
    return SlackInstallBroker(
        PostgresSlackChannelBrokerRepository(iam_connection),
        writer,
        SlackReferenceHasher(hmac_key),
        cipher,
        SlackProvider(
            client_id=os.environ["ATTUNE_SLACK_CLIENT_ID"],
            client_secret=client_secret,
            expected_app_id=os.environ["ATTUNE_SLACK_APP_ID"],
        ),
        redirect_uri=os.environ["ATTUNE_SLACK_REDIRECT_URI"],
    )


def create_production_app():
    hmac_key = _hmac_key(os.environ["ATTUNE_CHANNEL_HMAC_SECRET"])
    cipher = EnvelopeCipher(GoogleKmsKeyWrapper(os.environ["ATTUNE_CONNECTOR_KMS_KEY"]))
    writer = AuditWriterClient(os.environ["ATTUNE_AUDIT_WRITER_URL"])
    broker = GoogleChatLinkBroker(
        PostgresChannelBrokerRepository(iam_connection),
        writer,
        ChannelReferenceHasher(hmac_key),
        cipher,
        GoogleChatProvider(),
    )
    slack_broker = _slack_broker(hmac_key, cipher, writer)
    return create_app(
        broker,
        expected_audience=os.environ["ATTUNE_EXPECTED_AUDIENCE"],
        expected_ingress=os.environ["ATTUNE_INGRESS_SERVICE_ACCOUNT"],
        expected_control_plane=os.environ["ATTUNE_CONTROL_PLANE_SERVICE_ACCOUNT"],
        expected_worker=os.environ["ATTUNE_WORKER_SERVICE_ACCOUNT"],
        slack_broker=slack_broker,
        expected_slack_ingress=(
            os.environ["ATTUNE_SLACK_INGRESS_SERVICE_ACCOUNT"]
            if slack_broker is not None
            else None
        ),
    )


app = create_production_app()
