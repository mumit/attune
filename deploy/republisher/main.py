"""Thin republisher: Calendar webhook + Chat card-interaction endpoints
(docs/deployment.md §8, §12; docs/decisions.md).

A single, small, stateless Cloud Run service — the one exception to rule 5
(no inbound port on the credential-holding process). It holds no credentials,
no memory, and no configured OpenAI-compatible gateway token. Two routes, same shape: read an inbound
  webhook, forward the (verified, where applicable) payload onto a Pub/Sub
topic the main attune process pulls from, return an immediate response.

- ``/calendar-webhook``: Calendar push notifications carry almost no
  payload — just headers. No verification is needed here (the notification
  is treated as untrusted-origin input regardless; the main process only
  ever uses it as a signal to re-check its sync token against the real
  Calendar API, never as a direct command). If this route is ever abused,
  the blast radius is "the main process runs an extra, harmless
  reconciliation pass."

- ``/chat-interaction``: Google Chat messages and approve/reject buttons need a
  synchronous HTTP response, so resuming the paused LangGraph workflow can't
  happen here — that needs the checkpointer and memory store, which this
  service must never hold. Unlike the calendar route, this ONE DOES need
  request verification: without it, anyone who finds this service's public
  URL could forge an approve/reject decision on someone else's pending
  draft. Edit's dialog-open click never touches the graph, so it's answered
  directly here, synchronously, with no Pub/Sub involved.

Deliberately NOT part of the installable ``attune`` package — this is
deployable infrastructure, like ``deploy/mem0-compose.yml``, not application
code. Has its own ``requirements.txt``/``Dockerfile`` and is deployed
independently (``gcloud run deploy --source=deploy/republisher``).

Verification per https://developers.google.com/workspace/chat/verify-requests-from-chat,
using the "HTTP endpoint URL" Authentication Audience mode (the right choice
for a service that isn't using Cloud Run's own IAM-based auth, i.e. this
one, which is deployed with ``--allow-unauthenticated`` and does its own
verification): the bearer token is a Google-signed OIDC ID token whose
``aud`` claim equals the exact endpoint URL configured in the Chat app's
Connection settings (``<this service's URL>/chat-interaction`` — NOT the
project number; that value is only used in the alternative "Project Number"
audience mode, a different JWT-based verification path not implemented
here), and whose ``email`` claim — not ``iss`` — identifies the caller as
``chat@system.gserviceaccount.com``. Confirmed against current docs;
**not yet exercised against a live Chat app**.
"""

from __future__ import annotations

import json
import os

from flask import Flask, jsonify, request

app = Flask(__name__)

# Mirrors ingestion/chat_interactions.py's action names, channels/blocks.py's
# ACTION_EDIT/ACTION_EDIT_SUBMIT, and gchat_cards.py's EDIT_DIALOG_FIELD —
# duplicated rather than imported, since this service deliberately has no
# dependency on the attune package. Kept in sync by tests on both sides.
_ACTION_APPROVE = "attune_approve"
_ACTION_REJECT = "attune_reject"
_ACTION_EDIT = "attune_edit"
_ACTION_EDIT_SUBMIT = "attune_edit_submit"
_EDIT_DIALOG_FIELD = "attune_edit_text"

# The email claim on the verified ID token — NOT the "iss" claim, which is
# Google's own generic OIDC issuer, not the calling service account. See
# https://developers.google.com/workspace/chat/verify-requests-from-chat.
_CHAT_CALLER_EMAIL = "chat@system.gserviceaccount.com"


# ---------------------------------------------------------------------------
# Shared publish helper
# ---------------------------------------------------------------------------


def publish(publisher, topic: str, payload: dict) -> None:
    """Publish ``payload`` and wait for confirmation before acking the
    webhook. The caller expects a fast response, but silently losing a
    notification because we returned 200 before the publish actually landed
    would be worse than the extra latency of waiting for it."""
    future = publisher.publish(topic, json.dumps(payload).encode("utf-8"))
    future.result(timeout=10)


def _default_publisher():  # pragma: no cover - requires live GCP
    from google.cloud import pubsub_v1

    return pubsub_v1.PublisherClient()


# ---------------------------------------------------------------------------
# /calendar-webhook
# ---------------------------------------------------------------------------


def decode_headers(headers: dict) -> dict:
    """Extract the ``X-Goog-*`` notification headers Google sends.

    Mirrors ``ingestion/calendar_sync.py::decode_calendar_headers``'s shape
    exactly — that's what the main process expects to parse back out of the
    Pub/Sub message this service publishes.
    """
    return {
        "channel_id": headers.get("X-Goog-Channel-ID", ""),
        "resource_id": headers.get("X-Goog-Resource-ID", ""),
        "resource_state": headers.get("X-Goog-Resource-State", ""),
        "message_number": headers.get("X-Goog-Message-Number", ""),
    }


@app.route("/calendar-webhook", methods=["POST"])
def calendar_webhook():
    payload = decode_headers(request.headers)
    publisher = app.config.get("PUBLISHER")
    if publisher is None:  # pragma: no cover - requires live GCP
        publisher = _default_publisher()
    topic = app.config.get("TOPIC") or os.environ["CALENDAR_PUBSUB_TOPIC"]
    publish(publisher, topic, payload)
    return "", 200


# ---------------------------------------------------------------------------
# /chat-interaction
# ---------------------------------------------------------------------------


def verify_chat_request(headers, *, audience: str, verify_fn=None) -> bool:
    """Verify a request actually came from Google Chat.

    Google Chat signs its interaction webhook calls (when the Chat app's
    Authentication Audience is set to "HTTP endpoint URL") with a
    Google-issued OIDC ID token whose ``aud`` claim is this exact endpoint's
    URL and whose ``email`` claim is ``chat@system.gserviceaccount.com``.
    Verifying it here, before ever publishing to Pub/Sub, is what stops
    anyone who finds this service's public URL from forging approve/reject
    decisions on someone else's pending drafts — the async hand-off to the
    main process only helps if the thing handing off is trustworthy.
    """
    auth_header = headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header[len("Bearer "):]

    verify = verify_fn or _default_verify
    try:
        claims = verify(token, audience)
    except Exception:  # noqa: BLE001
        return False

    return claims.get("email") == _CHAT_CALLER_EMAIL


def _default_verify(token: str, audience: str):  # pragma: no cover - requires live Google
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, google_requests.Request(), audience)


@app.route("/chat-interaction", methods=["POST"])
def chat_interaction():
    # CHAT_APP_AUDIENCE must be the exact URL configured as this Chat app's
    # HTTP endpoint (e.g. "https://<service>.run.app/chat-interaction") —
    # that's the aud claim Google's ID token carries in "HTTP endpoint URL"
    # audience mode. See verify_chat_request's docstring.
    audience = app.config.get("CHAT_AUDIENCE") or os.environ.get("CHAT_APP_AUDIENCE", "")
    verify_fn = app.config.get("VERIFY_CHAT_FN")
    if not verify_chat_request(request.headers, audience=audience, verify_fn=verify_fn):
        return "", 403

    event = request.get_json(force=True, silent=True) or {}

    if event.get("type") == "MESSAGE":
        publisher = app.config.get("INTERACTION_PUBLISHER")
        if publisher is None:  # pragma: no cover - requires live GCP
            publisher = _default_publisher()
        topic = app.config.get("INTERACTION_TOPIC") or os.environ["CHAT_INTERACTION_PUBSUB_TOPIC"]
        publish(publisher, topic, event)
        return jsonify({"text": ""})

    action = event.get("action", {})
    fn = action.get("actionMethodName", "")

    if fn == _ACTION_EDIT:
        # Opening a dialog never touches the graph — answer immediately,
        # synchronously, no Pub/Sub involved. The dialog is prefilled from
        # the card echoed in the event itself (this service is stateless and
        # holds nothing to look a draft up in).
        thread_id = _action_param(action, "thread_id")
        if not thread_id:
            return "", 200
        return jsonify(
            _edit_dialog(thread_id, _draft_from_event(event) or "")
        )

    if fn in (_ACTION_APPROVE, _ACTION_REJECT, _ACTION_EDIT_SUBMIT):
        publisher = app.config.get("INTERACTION_PUBLISHER")
        if publisher is None:  # pragma: no cover - requires live GCP
            publisher = _default_publisher()
        topic = app.config.get("INTERACTION_TOPIC") or os.environ["CHAT_INTERACTION_PUBSUB_TOPIC"]
        publish(publisher, topic, event)
        if fn == _ACTION_EDIT_SUBMIT:
            # A dialog submit must be answered with an actionStatus (that's
            # what closes the dialog); the real confirmation is posted async
            # by the main process after it resumes the workflow.
            return jsonify({
                "actionResponse": {
                    "type": "DIALOG",
                    "dialogAction": {"actionStatus": {"statusCode": "OK"}},
                }
            })
        return jsonify({"text": "⏳ Processing your response..."})

    return "", 200


def _action_param(action: dict, key: str) -> str | None:
    for p in action.get("parameters", []):
        if p.get("key") == key:
            return p.get("value")
    return None


def _draft_from_event(event: dict) -> str | None:
    """The proposed draft = the first textParagraph of the echoed card
    (mirrors channels/gchat_cards.py's extract_draft_from_card_event)."""
    try:
        widgets = event["message"]["cardsV2"][0]["card"]["sections"][0]["widgets"]
    except (KeyError, IndexError, TypeError):
        return None
    for w in widgets:
        text = (w.get("textParagraph") or {}).get("text")
        if text:
            return text
    return None


def _edit_dialog(thread_id: str, proposed_draft: str) -> dict:
    """The edit dialog payload (mirrors channels/gchat_cards.py's
    edit_dialog — same field name, same submit action, kept in sync by
    tests on both sides)."""
    return {
        "actionResponse": {
            "type": "DIALOG",
            "dialogAction": {
                "dialog": {
                    "body": {
                        "sections": [
                            {
                                "header": "Edit draft",
                                "widgets": [
                                    {
                                        "textInput": {
                                            "name": _EDIT_DIALOG_FIELD,
                                            "label": "Your reply",
                                            "type": "MULTIPLE_LINE",
                                            "value": proposed_draft,
                                        }
                                    },
                                    {
                                        "buttonList": {
                                            "buttons": [
                                                {
                                                    "text": "Save & apply",
                                                    "onClick": {
                                                        "action": {
                                                            "function": _ACTION_EDIT_SUBMIT,
                                                            "parameters": [
                                                                {
                                                                    "key": "thread_id",
                                                                    "value": thread_id,
                                                                }
                                                            ],
                                                        }
                                                    },
                                                }
                                            ]
                                        }
                                    },
                                ],
                            }
                        ]
                    }
                }
            },
        }
    }


if __name__ == "__main__":  # pragma: no cover - requires a live run
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
