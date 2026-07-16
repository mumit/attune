"""Strict private HTTP adapter for mutation and fixed provider operations."""

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
    expected_worker: str,
    expected_oauth_exchange: str,
    token_verifier: Callable[[str, str], Mapping[str, Any]] | None = None,
):
    from flask import Flask, jsonify, request

    if not expected_audience.startswith("https://"):
        raise ValueError("expected audience must be HTTPS")
    if not expected_control_plane.endswith(".gserviceaccount.com"):
        raise ValueError("expected caller must be a service account")
    if not expected_worker.endswith(".gserviceaccount.com"):
        raise ValueError("expected caller must be a service account")
    if expected_worker == expected_control_plane:
        raise ValueError("control plane and worker identities must be distinct")
    if not expected_oauth_exchange.endswith(".gserviceaccount.com"):
        raise ValueError("expected caller must be a service account")
    if expected_oauth_exchange in {expected_control_plane, expected_worker}:
        raise ValueError("OAuth exchange identity must be distinct")
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_SECRET_REQUEST_BYTES
    verifier = token_verifier or _google_token_verifier

    def authorize(expected_service_account: str) -> bool:
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
        if not authorize(expected_control_plane):
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
        if not authorize(expected_control_plane):
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

    @app.post("/v1/oauth/google/exchange")
    def google_oauth_exchange():
        if not authorize(expected_oauth_exchange):
            return jsonify({"error": "forbidden"}), 403
        body = body_for(
            {
                "intent_id",
                "code",
                "pkce_verifier",
                "nonce_hash",
                "redirect_uri",
                "scopes",
            }
        )
        parsed = intent_id(body) if body is not None else None
        if parsed is None or not _oauth_exchange_body_is_valid(body):
            return jsonify({"error": "invalid_request"}), 400
        try:
            result = broker.google_oauth_exchange(
                parsed,
                authorization_code=body["code"],
                pkce_verifier=body["pkce_verifier"],
                nonce_hash=bytes.fromhex(body["nonce_hash"]),
                redirect_uri=body["redirect_uri"],
                scopes=tuple(body["scopes"]),
            )
        except Exception as error:
            LOG.warning("OAuth credential exchange failed (%s)", type(error).__name__)
            return jsonify({"error": "broker_unavailable"}), 503
        return ("", result.status_code)

    @app.post("/v1/providers/google/gmail/profile")
    def google_gmail_profile():
        if not authorize(expected_worker):
            return jsonify({"error": "forbidden"}), 403
        body = body_for({"intent_id"})
        parsed = intent_id(body) if body is not None else None
        if parsed is None:
            return jsonify({"error": "invalid_request"}), 400
        try:
            result = broker.google_gmail_profile(parsed)
        except Exception as error:
            LOG.warning("credential use failed (%s)", type(error).__name__)
            return jsonify({"error": "broker_unavailable"}), 503
        if result.status_code != 200:
            # Fixed signal only: never include intent, tenant, connector,
            # credential, provider response, or exception detail.
            LOG.warning(
                "attune_secret_broker_use_anomaly status=%d",
                result.status_code,
            )
        if result.status_code == 200 and result.body is not None:
            return jsonify(result.body), 200
        return ("", result.status_code)

    @app.post("/v1/providers/google/calendar/primary")
    def google_calendar_primary():
        if not authorize(expected_worker):
            return jsonify({"error": "forbidden"}), 403
        body = body_for({"intent_id"})
        parsed = intent_id(body) if body is not None else None
        if parsed is None:
            return jsonify({"error": "invalid_request"}), 400
        try:
            result = broker.google_calendar_primary(parsed)
        except Exception as error:
            LOG.warning("credential use failed (%s)", type(error).__name__)
            return jsonify({"error": "broker_unavailable"}), 503
        if result.status_code != 204:
            LOG.warning(
                "attune_secret_broker_use_anomaly status=%d",
                result.status_code,
            )
        return ("", result.status_code)

    return app


def _oauth_exchange_body_is_valid(body: Mapping[str, Any]) -> bool:
    code = body.get("code")
    verifier = body.get("pkce_verifier")
    nonce_hash = body.get("nonce_hash")
    redirect_uri = body.get("redirect_uri")
    scopes = body.get("scopes")
    return (
        isinstance(code, str)
        and 1 <= len(code) <= 4096
        and all(0x21 <= ord(character) <= 0x7E for character in code)
        and isinstance(verifier, str)
        and 43 <= len(verifier) <= 128
        and verifier.replace("-", "A").replace("_", "A").isalnum()
        and isinstance(nonce_hash, str)
        and len(nonce_hash) == 64
        and all(character in "0123456789abcdef" for character in nonce_hash)
        and isinstance(redirect_uri, str)
        and redirect_uri.startswith("https://")
        and len(redirect_uri) <= 2048
        and isinstance(scopes, list)
        and 1 <= len(scopes) <= 32
        and len(set(scopes)) == len(scopes)
        and all(isinstance(scope, str) and 1 <= len(scope) <= 255 for scope in scopes)
    )
