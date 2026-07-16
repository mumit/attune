"""Authenticated private HTTP boundary for channel link consumption."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping
from uuid import UUID

from .channel_broker import GoogleChatLinkBroker
from .task_envelope import _google_token_verifier, _verify_claims

LOG = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 2048


def create_app(
    broker: GoogleChatLinkBroker,
    *,
    expected_audience: str,
    expected_ingress: str,
    expected_control_plane: str,
    token_verifier: Callable[[str, str], Mapping[str, Any]] | None = None,
):
    from flask import Flask, jsonify, request

    if not expected_audience.startswith("https://"):
        raise ValueError("expected audience must be HTTPS")
    if not expected_ingress.endswith(".gserviceaccount.com"):
        raise ValueError("expected ingress must be a service account")
    if not expected_control_plane.endswith(".gserviceaccount.com"):
        raise ValueError("expected control plane must be a service account")
    verifier = token_verifier or _google_token_verifier
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

    def authorized(expected_service_account: str) -> bool:
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
                expected_service_account=expected_service_account,
                now=int(time.time()),
            )
            return True
        except Exception:
            return False

    @app.get("/healthz")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/v1/google-chat/link-owner-dm")
    def link_owner_dm():
        if not authorized(expected_ingress):
            return jsonify({"error": "forbidden"}), 403
        if not request.is_json:
            return jsonify({"error": "invalid_request"}), 400
        body = request.get_json(silent=True)
        expected = {"version", "link_code", "app_ref", "actor_ref", "destination_ref"}
        if not isinstance(body, dict) or set(body) != expected or body["version"] != 1:
            return jsonify({"error": "invalid_request"}), 400
        try:
            result = broker.link_owner_dm(
                link_code=body["link_code"],
                app_ref=body["app_ref"],
                actor_ref=body["actor_ref"],
                destination_ref=body["destination_ref"],
            )
        except ValueError:
            return jsonify({"error": "invalid_request"}), 400
        except Exception as error:
            LOG.warning("channel link failed (%s)", type(error).__name__)
            return jsonify({"error": "link_unavailable"}), 503
        return jsonify(
            {"status": "linked", "destination_status": result.destination_status}
        )

    @app.post("/v1/google-chat/test-delivery")
    def test_delivery():
        if not authorized(expected_control_plane):
            return jsonify({"error": "forbidden"}), 403
        if not request.is_json:
            return jsonify({"error": "invalid_request"}), 400
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {"version", "destination_id"}:
            return jsonify({"error": "invalid_request"}), 400
        if body.get("version") != 1:
            return jsonify({"error": "invalid_request"}), 400
        try:
            destination_id = UUID(body["destination_id"])
            result = broker.test_delivery(destination_id=destination_id)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid_request"}), 400
        except Exception as error:
            LOG.warning("channel delivery test failed (%s)", type(error).__name__)
            return jsonify({"error": "delivery_unavailable"}), 503
        return jsonify(
            {"status": "delivered", "destination_status": result.destination_status}
        )

    return app
