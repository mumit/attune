"""Authenticated private HTTP boundary for channel link consumption."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping
from uuid import UUID

from .channel_broker import GoogleChatLinkBroker
from .task_envelope import _google_token_verifier, _verify_claims

LOG = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 12_288


def create_app(
    broker: GoogleChatLinkBroker,
    *,
    expected_audience: str,
    expected_ingress: str,
    expected_control_plane: str,
    expected_worker: str,
    token_verifier: Callable[[str, str], Mapping[str, Any]] | None = None,
):
    from flask import Flask, jsonify, request

    if not expected_audience.startswith("https://"):
        raise ValueError("expected audience must be HTTPS")
    if not expected_ingress.endswith(".gserviceaccount.com"):
        raise ValueError("expected ingress must be a service account")
    if not expected_control_plane.endswith(".gserviceaccount.com"):
        raise ValueError("expected control plane must be a service account")
    if not expected_worker.endswith(".gserviceaccount.com"):
        raise ValueError("expected worker must be a service account")
    if len({expected_ingress, expected_control_plane, expected_worker}) != 3:
        raise ValueError("channel broker callers must use distinct identities")
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

    @app.post("/v1/google-chat/accept-message")
    def accept_message():
        if not authorized(expected_ingress):
            return jsonify({"error": "forbidden"}), 403
        if not request.is_json:
            return jsonify({"error": "invalid_request"}), 400
        body = request.get_json(silent=True)
        expected = {
            "version", "app_ref", "actor_ref", "destination_ref",
            "message_ref", "text",
        }
        if not isinstance(body, dict) or set(body) != expected or body.get("version") != 1:
            return jsonify({"error": "invalid_request"}), 400
        try:
            result = broker.accept_message(
                app_ref=body["app_ref"], actor_ref=body["actor_ref"],
                destination_ref=body["destination_ref"],
                message_ref=body["message_ref"], text=body["text"],
            )
        except ValueError:
            return jsonify({"error": "invalid_request"}), 400
        except Exception as error:
            LOG.warning("channel message acceptance failed (%s)", type(error).__name__)
            return jsonify({"error": "message_unavailable"}), 503
        return jsonify({
            "status": "accepted",
            "dispatch_intent_id": str(result.dispatch_intent_id),
            "accepted_new": result.accepted_new,
        })

    @app.post("/v1/google-chat/deliver-reply")
    def deliver_reply():
        if not authorized(expected_worker):
            return jsonify({"error": "forbidden"}), 403
        if not request.is_json:
            return jsonify({"error": "invalid_request"}), 400
        body = request.get_json(silent=True)
        if (
            not isinstance(body, dict)
            or set(body) != {"version", "destination_id", "job_id"}
            or body.get("version") != 1
        ):
            return jsonify({"error": "invalid_request"}), 400
        try:
            destination_id = UUID(body["destination_id"])
            job_id = UUID(body["job_id"])
            if str(destination_id) != body["destination_id"] or str(job_id) != body["job_id"]:
                raise ValueError("non-canonical UUID")
            delivered = broker.deliver_reply(
                destination_id=destination_id, job_id=job_id
            )
        except (TypeError, ValueError):
            return jsonify({"error": "invalid_request"}), 400
        except Exception as error:
            LOG.warning("channel reply failed (%s)", type(error).__name__)
            return jsonify({"error": "delivery_unavailable"}), 503
        return jsonify({"status": "delivered"}) if delivered else (
            jsonify({"error": "delivery_unavailable"}), 503
        )

    return app
