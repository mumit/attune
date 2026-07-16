"""Public but provider-authenticated Google Chat setup ingress."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping, Protocol

from .google_chat_ingress import (
    decode_owner_dm_link_diagnostic,
    decode_owner_dm_message_diagnostic,
)
from .task_envelope import _google_token_verifier, _verify_claims

LOG = logging.getLogger(__name__)
CHAT_CALLER = "chat@system.gserviceaccount.com"
MAX_REQUEST_BYTES = 16_384


class ChannelBroker(Protocol):
    def link_google_chat_owner_dm(self, **kwargs) -> bool: ...


def create_app(
    broker: ChannelBroker,
    *,
    expected_audience: str,
    app_project_number: str,
    token_verifier: Callable[[str, str], Mapping[str, Any]] | None = None,
):
    from flask import Flask, jsonify, request

    if not expected_audience.startswith("https://"):
        raise ValueError("Google Chat audience must be an exact HTTPS URL")
    if not app_project_number.isdigit() or not 6 <= len(app_project_number) <= 21:
        raise ValueError("Google Chat project number is invalid")
    verifier = token_verifier or _google_token_verifier
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

    def authorized() -> bool:
        header = request.headers.get("Authorization", "")
        if len(header) > 16_384 or not header.startswith("Bearer "):
            return False
        token = header[7:]
        if not token or any(character.isspace() for character in token):
            return False
        try:
            claims = verifier(token, expected_audience)
            _verify_claims(
                claims,
                expected_audience=expected_audience,
                expected_service_account=CHAT_CALLER,
                now=int(time.time()),
            )
            return True
        except Exception:
            return False

    @app.get("/healthz")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/v1/provider/google-chat/events")
    def google_chat_event():
        if not authorized():
            return jsonify({"error": "forbidden"}), 403
        if not request.is_json:
            return jsonify({"error": "invalid_request"}), 400
        event = request.get_json(silent=True)
        message, rejection = decode_owner_dm_message_diagnostic(event)
        if message is None:
            LOG.warning(
                "Google Chat event did not match owner DM (%s)", rejection
            )
            return jsonify({"text": "Attune accepts messages only in the verified owner direct message."})
        link, rejection = decode_owner_dm_link_diagnostic(event)
        if link is None:
            if message.text.startswith(("/link", " /link")):
                LOG.warning(
                    "Google Chat event did not match owner-DM link (%s)", rejection
                )
                return jsonify({"text": "Send /link followed by your one-time Attune code in a direct message."})
            return jsonify(
                {
                    "text": (
                        "Attune conversations are not active in this development environment yet. "
                        "Your verified Chat connection does not need a new link code."
                    )
                }
            )
        try:
            linked = broker.link_google_chat_owner_dm(
                link_code=link.link_code,
                app_ref=f"projects/{app_project_number}",
                actor_ref=link.actor_ref,
                destination_ref=link.destination_ref,
            )
        except Exception as error:
            LOG.warning("Google Chat link ingress failed (%s)", type(error).__name__)
            linked = False
        if not linked:
            return jsonify({"text": "Attune could not use that code. Create a new code and try again."})
        return jsonify({"text": "Attune is connected. Return to the setup page to continue."})

    return app
