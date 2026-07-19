"""Private authenticated HTTP adapter for fixed hosted model tasks."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping

from .model_gateway import PROFILE_NAME, HostedModelGateway
from .task_envelope import _google_token_verifier, _verify_claims

LOG = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 40_000


def _usage_body(usage) -> dict[str, int] | None:
    if usage is None:
        return None
    return {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}


def create_app(
    gateway: HostedModelGateway,
    *,
    expected_audience: str,
    expected_worker: str,
    token_verifier: Callable[[str, str], Mapping[str, Any]] | None = None,
    profiles_enabled: bool = False,
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

    def _profile_from(body: dict) -> str | None:
        """Extracts the optional bounded ``profile`` field. Returns ``None``
        when absent (byte-identical to the pre-profile envelope). The field
        is only ever accepted when THIS gateway instance's own
        ATTUNE_ENABLE_TENANT_MODEL_PROFILES gate is on -- independent
        defense-in-depth from the worker's own gate, so a request carrying
        the field while this gate is off is refused as invalid_request
        rather than silently honored or silently ignored."""
        if "profile" not in body:
            return None
        if not profiles_enabled:
            raise ValueError("model profile field is not enabled")
        profile = body["profile"]
        if not isinstance(profile, str) or not PROFILE_NAME.fullmatch(profile):
            raise ValueError("model profile field is invalid")
        return profile

    @app.post("/v1/models/complete")
    def complete():
        if not authorized():
            return jsonify({"error": "forbidden"}), 403
        if not request.is_json:
            return jsonify({"error": "invalid_request"}), 400
        body = request.get_json(silent=True)
        if (
            not isinstance(body, dict)
            or body.get("version") != 1
            or set(body) not in ({"version", "task", "messages"}, {"version", "task", "messages", "profile"})
            or (not profiles_enabled and "profile" in body)
        ):
            return jsonify({"error": "invalid_request"}), 400
        try:
            profile = _profile_from(body)
            result = gateway.complete(
                task=body["task"], messages=body["messages"], profile=profile
            )
        except ValueError:
            return jsonify({"error": "invalid_request"}), 400
        except Exception as error:
            LOG.warning("model completion failed (%s)", type(error).__name__)
            return jsonify({"error": "model_unavailable"}), 503
        return jsonify({"text": result.text, "usage": _usage_body(result.usage)})

    @app.post("/v1/models/embed")
    def embed():
        if not authorized():
            return jsonify({"error": "forbidden"}), 403
        if not request.is_json:
            return jsonify({"error": "invalid_request"}), 400
        body = request.get_json(silent=True)
        if (
            not isinstance(body, dict)
            or body.get("version") != 1
            or body.get("task") != "embed"
            or set(body) not in ({"version", "task", "input"}, {"version", "task", "input", "profile"})
            or (not profiles_enabled and "profile" in body)
        ):
            return jsonify({"error": "invalid_request"}), 400
        try:
            profile = _profile_from(body)
            result = gateway.embed(text=body["input"], profile=profile)
        except ValueError:
            return jsonify({"error": "invalid_request"}), 400
        except Exception as error:
            LOG.warning("model embedding failed (%s)", type(error).__name__)
            return jsonify({"error": "model_unavailable"}), 503
        return jsonify({"vector": list(result.vector), "usage": _usage_body(result.usage)})

    return app
