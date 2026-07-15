"""Strict private HTTP adapter for credential installation and revocation."""

from __future__ import annotations

import time
import logging
from typing import Any, Callable, Mapping
from uuid import UUID

from .secret_broker import SecretBroker
from .task_envelope import _google_token_verifier, _verify_claims

MAX_SECRET_REQUEST_BYTES = 70_000
LOG = logging.getLogger(__name__)


def create_app(
    broker: SecretBroker,
    *,
    expected_audience: str,
    expected_control_plane: str,
    token_verifier: Callable[[str, str], Mapping[str, Any]] | None = None,
):
    from flask import Flask, jsonify, request

    if not expected_audience.startswith("https://"):
        raise ValueError("expected audience must be HTTPS")
    if not expected_control_plane.endswith(".gserviceaccount.com"):
        raise ValueError("expected caller must be a service account")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_SECRET_REQUEST_BYTES
    verifier = token_verifier or _google_token_verifier

    def authorize() -> bool:
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
                expected_service_account=expected_control_plane,
                now=int(time.time()),
            )
        except Exception:
            return False
        return True

    def body_for(keys: set[str]):
        if not request.is_json:
            return None
        body = request.get_json(silent=True)
        return body if isinstance(body, dict) and set(body) == keys else None

    def intent_id(body):
        raw = body.get("intent_id")
        if not isinstance(raw, str):
            return None
        try:
            parsed = UUID(raw)
        except ValueError:
            return None
        return parsed if str(parsed) == raw else None

    @app.get("/healthz")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/v1/credentials/install")
    def install():
        if not authorize():
            return jsonify({"error": "forbidden"}), 403
        body = body_for({"intent_id", "credential"})
        parsed = intent_id(body) if body is not None else None
        if parsed is None or not isinstance(body["credential"], dict):
            return jsonify({"error": "invalid_request"}), 400
        try:
            result = broker.install(parsed, body["credential"])
        except Exception as error:
            LOG.warning("credential install failed (%s)", type(error).__name__)
            return jsonify({"error": "broker_unavailable"}), 503
        return ("", result.status_code)

    @app.post("/v1/credentials/revoke")
    def revoke():
        if not authorize():
            return jsonify({"error": "forbidden"}), 403
        body = body_for({"intent_id"})
        parsed = intent_id(body) if body is not None else None
        if parsed is None:
            return jsonify({"error": "invalid_request"}), 400
        try:
            result = broker.revoke(parsed)
        except Exception as error:
            LOG.warning("credential revoke failed (%s)", type(error).__name__)
            return jsonify({"error": "broker_unavailable"}), 503
        return ("", result.status_code)

    return app
