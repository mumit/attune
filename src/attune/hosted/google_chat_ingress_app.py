"""Production composition root for verified Google Chat setup ingress."""

from __future__ import annotations

import os

from .channel_broker_client import ChannelBrokerClient
from .google_chat_ingress_service import create_app


def create_production_app():
    return create_app(
        ChannelBrokerClient(
            os.environ["ATTUNE_CHANNEL_BROKER_URL"],
            os.environ["ATTUNE_CHANNEL_BROKER_AUDIENCE"],
        ),
        expected_audience=os.environ["ATTUNE_GOOGLE_CHAT_AUDIENCE"],
        app_project_number=os.environ["ATTUNE_GOOGLE_CHAT_PROJECT_NUMBER"],
    )


app = create_production_app()
