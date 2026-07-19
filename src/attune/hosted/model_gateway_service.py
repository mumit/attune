"""Private authenticated HTTP adapter for fixed hosted model tasks."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping

from .model_gateway import HostedModelGateway
from .task_envelope import _google_token_verifier, _verify_claims

LOG = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 40_000


def create_app(
    gateway: HostedModelGateway,
    *,
    expected_audience: str,
    expected_worker: str,
    token_verifier: Callable[[str, str], Mapping[str, Any]] | None = None,
):
    from flask import Flask, jsonify, request

    if not expected_audience.startswith("https://"):
        raise ValueError("model gateway audience must be HTTPS")
    if not expected_worker.endswith(".gserviceaccount.com"):
        raise ValueError("expected worker must be a service account")
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
                expected_service_account=expected_worker,
                now=int(time.time()),
            )
            return True
        except Exception:
            return False

    @app.get("/healthz")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/v1/models/complete")
    def complete():
        if not authorized():
            return jsonify({"error": "forbidden"}), 403
        if not request.is_json:
            return jsonify({"error": "invalid_request"}), 400
        body = request.get_json(silent=True)
        if (
            not isinstance(body, dict)
            or set(body) != {"version", "task", "messages"}
            or body.get("version") != 1
        ):
            return jsonify({"error": "invalid_request"}), 400
        try:
            result = gateway.complete(task=body["task"], messages=body["messages"])
        except ValueError:
            return jsonify({"error": "invalid_request"}), 400
        except Exception as error:
            LOG.warning("model completion failed (%s)", type(error).__name__)
            return jsonify({"error": "model_unavailable"}), 503
        return jsonify({"text": result.text})

    @app.post("/v1/models/embed")
    def embed():
        if not authorized():
            return jsonify({"error": "forbidden"}), 403
        if not request.is_json:
            return jsonify({"error": "invalid_request"}), 400
        body = request.get_json(silent=True)
        if (
            not isinstance(body, dict)
            or set(body) != {"version", "task", "input"}
            or body.get("version") != 1
            or body.get("task") != "embed"
        ):
            return jsonify({"error": "invalid_request"}), 400
        try:
            result = gateway.embed(text=body["input"])
        except ValueError:
            return jsonify({"error": "invalid_request"}), 400
        except Exception as error:
            LOG.warning("model embedding failed (%s)", type(error).__name__)
            return jsonify({"error": "model_unavailable"}), 503
        return jsonify({"vector": list(result.vector)})

    return app
