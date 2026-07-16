"""Composition root for the dormant hosted OAuth callback scrubber."""

from __future__ import annotations

import os

from .oauth_callback_service import create_app
from .oauth_exchange_client import PrivateOAuthExchangeClient


def create_production_app():
    enabled_value = os.environ.get("ATTUNE_GOOGLE_OAUTH_ENABLED", "false")
    if enabled_value not in {"true", "false"}:
        raise ValueError("ATTUNE_GOOGLE_OAUTH_ENABLED must be true or false")
    enabled = enabled_value == "true"
    return create_app(
        os.environ["ATTUNE_PUBLIC_HOST"],
        oauth_enabled=enabled,
        exchange=(
            PrivateOAuthExchangeClient(
                os.environ["ATTUNE_OAUTH_EXCHANGE_URL"],
                os.environ["ATTUNE_OAUTH_EXCHANGE_AUDIENCE"],
            )
            if enabled
            else None
        ),
    )


app = create_production_app()
