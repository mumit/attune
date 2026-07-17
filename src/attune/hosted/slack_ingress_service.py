"""Public but signature-authenticated Slack event ingress."""

from __future__ import annotations

import json
import logging
import time
from typing import Protocol

from .slack_ingress import (
    decode_owner_dm_message_diagnostic,
    decode_url_verification,
    verify_slack_signature,
)

LOG = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 65_536


class ChannelBroker(Protocol):
    def accept_slack_message(self, **kwargs): ...


class DispatchBroker(Protocol):
    def dispatch(self, intent_id) -> bool: ...


def create_app(
    broker: ChannelBroker,
    *,
    signing_secret: bytes,
    dispatch_broker: DispatchBroker | None = None,
    conversations_enabled: bool = False,
    clock=None,
):
    from flask import Flask, jsonify, request

    if not isinstance(signing_secret, bytes) or not 8 <= len(signing_secret) <= 128:
        raise ValueError("Slack signing secret is invalid")
    now = clock or (lambda: int(time.time()))
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

    @app.get("/healthz")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/v1/provider/slack/events")
    def slack_event():
        raw_body = request.get_data(cache=False, as_text=False)
        if not verify_slack_signature(
            signing_secret=signing_secret,
            timestamp_header=request.headers.get("X-Slack-Request-Timestamp"),
            signature_header=request.headers.get("X-Slack-Signature"),
            raw_body=raw_body,
            now=now(),
        ):
            return jsonify({"error": "forbidden"}), 403
        try:
            payload = json.loads(raw_body)
        except ValueError:
            return jsonify({"error": "invalid_request"}), 400
        handshake = decode_url_verification(payload)
        if handshake is not None:
            return jsonify({"challenge": handshake.challenge})
        message, rejection = decode_owner_dm_message_diagnostic(payload)
        if message is None:
            # Slack retries non-200 responses; acknowledge and drop anything
            # that is not a plain owner direct message.
            LOG.info("Slack event did not match owner DM (%s)", rejection)
            return jsonify({"ok": True})
        if not conversations_enabled or dispatch_broker is None:
            LOG.info("Slack conversation ingress is not active")
            return jsonify({"ok": True})
        try:
            intent_id = broker.accept_slack_message(
                team_ref=message.team_ref,
                actor_ref=message.actor_ref,
                destination_ref=message.destination_ref,
                message_ref=message.message_ref,
                text=message.text,
            )
            dispatched = dispatch_broker.dispatch(intent_id)
        except Exception as error:
            LOG.warning("Slack message ingress failed (%s)", type(error).__name__)
            dispatched = False
        if not dispatched:
            LOG.warning("Slack message was not dispatched")
        return jsonify({"ok": True})

    return app
