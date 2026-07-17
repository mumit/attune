"""Production composition root for signature-verified Slack ingress."""

from __future__ import annotations

import os

from .channel_broker_client import ChannelBrokerClient
from .dispatch_broker_client import DispatchBrokerClient
from .slack_ingress_service import create_app


def _signing_secret(secret_resource: str) -> bytes:
    from google.cloud import secretmanager

    response = secretmanager.SecretManagerServiceClient().access_secret_version(
        request={"name": f"{secret_resource}/versions/latest"}
    )
    secret = response.payload.data.strip()
    if not 8 <= len(secret) <= 128:
        raise RuntimeError("Slack signing secret is invalid")
    return secret


def create_production_app():
    return create_app(
        ChannelBrokerClient(
            os.environ["ATTUNE_CHANNEL_BROKER_URL"],
            os.environ["ATTUNE_CHANNEL_BROKER_AUDIENCE"],
        ),
        signing_secret=_signing_secret(os.environ["ATTUNE_SLACK_SIGNING_SECRET"]),
        dispatch_broker=DispatchBrokerClient(
            os.environ["ATTUNE_DISPATCH_BROKER_URL"],
            os.environ["ATTUNE_DISPATCH_BROKER_AUDIENCE"],
        ) if os.environ.get("ATTUNE_ENABLE_SLACK_CONVERSATION") == "true" else None,
        conversations_enabled=(
            os.environ.get("ATTUNE_ENABLE_SLACK_CONVERSATION") == "true"
        ),
    )


app = create_production_app()
