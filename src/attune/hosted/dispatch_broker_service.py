"""Strict private HTTP boundary for opaque dispatch intents."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping, Sequence, Union
from uuid import UUID

from .dispatch_broker import DispatchBroker
from .task_envelope import _google_token_verifier, _verify_claims

LOG = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 1024
PRODUCER_KINDS = frozenset({"control_plane", "ingress", "worker"})

CallerEmails = Union[str, Sequence[str]]


def create_app(
    broker: DispatchBroker,
    *,
    expected_audience: str,
    expected_callers: Mapping[str, CallerEmails],
    token_verifier: Callable[[str, str], Mapping[str, Any]] | None = None,
):
    from flask import Flask, jsonify, request

    if not expected_audience.startswith("https://"):
        raise ValueError("expected audience must be HTTPS")
    if set(expected_callers) != PRODUCER_KINDS:
        raise ValueError("every dispatch producer identity must be configured")
    normalized_callers: dict[str, tuple[str, ...]] = {
        kind: ((emails,) if isinstance(emails, str) else tuple(emails))
        for kind, emails in expected_callers.items()
    }
    all_emails = [email for emails in normalized_callers.values() for email in emails]
    if not all_emails or any(
        not email.endswith(".gserviceaccount.com") for email in all_emails
    ) or any(not emails for emails in normalized_callers.values()):
        raise ValueError("dispatch producer identities must be distinct service accounts")
    if len(set(all_emails)) != len(all_emails):
        raise ValueError("dispatch producer identities must be distinct service accounts")
    caller_kinds = {
        email: kind for kind, emails in normalized_callers.items() for email in emails
    }
    verifier = token_verifier or _google_token_verifier
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

    def authorize() -> str | None:
        header = request.headers.get("Authorization", "")
        if len(header) > 16_384 or not header.startswith("Bearer "):
            return None
        token = header[7:]
        if not token or any(character.isspace() for character in token):
            return None
        try:
            claims = verifier(token, expected_audience)
            email = claims.get("email")
            producer_kind = caller_kinds.get(email)
            if producer_kind is None:
                return None
            _verify_claims(
                claims,
                expected_audience=expected_audience,
                expected_service_account=email,
                now=int(time.time()),
            )
            return producer_kind
        except Exception:
            return None

    @app.get("/healthz")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/v1/dispatch-intents/dispatch")
    def dispatch():
        producer_kind = authorize()
        if producer_kind is None:
            return jsonify({"error": "forbidden"}), 403
        if not request.is_json:
            return jsonify({"error": "invalid_request"}), 400
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {"intent_id"}:
            return jsonify({"error": "invalid_request"}), 400
        raw_id = body["intent_id"]
        if not isinstance(raw_id, str):
            return jsonify({"error": "invalid_request"}), 400
        try:
            intent_id = UUID(raw_id)
        except ValueError:
            return jsonify({"error": "invalid_request"}), 400
        if str(intent_id) != raw_id:
            return jsonify({"error": "invalid_request"}), 400
        try:
            result = broker.dispatch(intent_id, producer_kind=producer_kind)
        except Exception as error:
            LOG.warning("dispatch failed (%s)", type(error).__name__)
            return jsonify({"error": "broker_unavailable"}), 503
        return ("", result.status_code)

    return app
