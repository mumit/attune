from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

pytest.importorskip("flask")

from attune.hosted.control_plane_service import create_app
from attune.hosted.identity_session import IdentitySession
from attune.hosted.tenant import TenantContext

HOST = "dev.attune.mumit.org"
PROJECT = "attune-development-502421"
API_KEY = "AIza" + "a" * 35
AUTH_DOMAIN = f"{PROJECT}.firebaseapp.com"
TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")
PRINCIPAL_ID = UUID("20000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("30000000-0000-4000-8000-000000000001")
CONVERSATION_ID = UUID("40000000-0000-4000-8000-000000000001")


class Sessions:
    def __init__(self, opened=True):
        self.session = (
            IdentitySession(SESSION_ID, TenantContext(TENANT_ID), PRINCIPAL_ID)
            if opened
            else None
        )
        self.calls = []

    def open(self, identity, session_secrets, *, expires_at):
        self.calls.append(("open", identity, session_secrets, expires_at))
        return self.session

    def read(self, token):
        self.calls.append(("read", token))
        return self.session

    def authorize(self, token, csrf):
        self.calls.append(("authorize", token, csrf))
        return self.session

    def authorize_recent(self, token, csrf):
        self.calls.append(("authorize_recent", token, csrf))
        return self.session

    def revoke(self, token, csrf):
        self.calls.append(("revoke", token, csrf))
        return bool(self.session)


class WebConversation:
    def __init__(self, send_failure=None, turns_result=((), False)):
        self.calls = []
        self.send_failure = send_failure
        self.turns_result = turns_result

    def send(self, context, **kwargs):
        self.calls.append(("send", context, kwargs))
        if self.send_failure:
            raise self.send_failure
        return type(
            "Accepted",
            (),
            {"conversation_id": CONVERSATION_ID, "user_sequence": 1},
        )()

    def turns(self, context, **kwargs):
        self.calls.append(("turns", context, kwargs))
        return self.turns_result


def verified(_token, project_id):
    from attune.hosted.identity import VerifiedIdentity

    assert project_id == PROJECT
    return VerifiedIdentity(
        issuer=f"https://securetoken.google.com/{PROJECT}",
        subject_hash=bytes.fromhex("11" * 32),
        authenticated_at=datetime.now(timezone.utc),
    )


def same_origin():
    return {"Origin": f"https://{HOST}", "Sec-Fetch-Site": "same-origin"}


def identity_client(sessions=None, verifier=verified, **kwargs):
    return create_app(
        HOST,
        identity_enabled=True,
        project_id=PROJECT,
        identity_api_key=API_KEY,
        identity_auth_domain=AUTH_DOMAIN,
        sessions=sessions or Sessions(),
        token_verifier=verifier,
        **kwargs,
    ).test_client()


def signed_in_client(**kwargs):
    sessions = kwargs.pop("sessions", Sessions())
    client = identity_client(sessions, **kwargs)
    bootstrap = client.get(
        "/v1/session/bootstrap", base_url=f"https://{HOST}"
    ).get_json()
    response = client.post(
        "/v1/session",
        json={"id_token": "signed", "login_challenge": bootstrap["login_challenge"]},
        headers=same_origin(),
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 200
    return client, sessions


def test_web_conversation_routes_are_absent_when_the_gate_is_off():
    client, _sessions = signed_in_client()
    assert client.post(
        "/v1/conversation/messages", json={"schema_version": 1, "text": "hi"},
        base_url=f"https://{HOST}",
    ).status_code == 404
    assert client.get(
        "/v1/conversation/turns", base_url=f"https://{HOST}"
    ).status_code == 404


def test_web_conversation_send_requires_ordinary_session_origin_and_csrf():
    conversation = WebConversation()
    client, _sessions = signed_in_client(
        hosted_web_conversation_enabled=True, web_conversation=conversation,
    )
    # No cookies/CSRF at all -> unauthenticated.
    import werkzeug.test

    anonymous = werkzeug.test.Client(client.application)
    refused = anonymous.post(
        "/v1/conversation/messages",
        json={"schema_version": 1, "text": "hi"},
        headers=same_origin(),
        base_url=f"https://{HOST}",
    )
    assert refused.status_code == 401
    assert conversation.calls == []

    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value

    # Cross-origin is refused even with a valid session and CSRF token.
    cross_origin = client.post(
        "/v1/conversation/messages",
        json={"schema_version": 1, "text": "hi"},
        headers={"Origin": "https://evil.example", "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert cross_origin.status_code == 401
    assert conversation.calls == []

    # Missing/incorrect CSRF header is refused.
    missing_csrf = client.post(
        "/v1/conversation/messages",
        json={"schema_version": 1, "text": "hi"},
        headers=same_origin(),
        base_url=f"https://{HOST}",
    )
    assert missing_csrf.status_code == 401
    assert conversation.calls == []


def test_web_conversation_send_accepts_and_dispatches():
    conversation = WebConversation()
    client, _sessions = signed_in_client(
        hosted_web_conversation_enabled=True, web_conversation=conversation,
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = client.post(
        "/v1/conversation/messages",
        json={"schema_version": 1, "text": "What is on my calendar?"},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 202
    assert response.get_json() == {
        "schema_version": 1,
        "conversation": str(CONVERSATION_ID),
        "user_sequence": 1,
        "state": "accepted",
    }
    [(_, context, kwargs)] = conversation.calls
    assert context == TenantContext(TENANT_ID)
    assert kwargs == {
        "principal_id": PRINCIPAL_ID,
        "session_id": SESSION_ID,
        "text": "What is on my calendar?",
    }


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"schema_version": 2, "text": "hi"},
        {"schema_version": 1, "text": ""},
        {"schema_version": 1, "text": "x" * 8_001},
        {"schema_version": 1, "text": 5},
        {"schema_version": 1},
        {"schema_version": 1, "text": "hi", "extra": True},
    ],
)
def test_web_conversation_send_refuses_a_malformed_body(body):
    conversation = WebConversation()
    client, _sessions = signed_in_client(
        hosted_web_conversation_enabled=True, web_conversation=conversation,
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = client.post(
        "/v1/conversation/messages",
        json=body,
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 400
    assert conversation.calls == []


def test_web_conversation_send_dispatch_failure_is_a_bounded_error():
    conversation = WebConversation(send_failure=RuntimeError("dispatch refused"))
    client, _sessions = signed_in_client(
        hosted_web_conversation_enabled=True, web_conversation=conversation,
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = client.post(
        "/v1/conversation/messages",
        json={"schema_version": 1, "text": "hi"},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 503
    assert response.get_json() == {"error": "conversation_unavailable"}
    assert b"dispatch refused" not in response.data


def test_web_conversation_turns_reads_with_an_ordinary_session_and_paginates():
    conversation = WebConversation(
        turns_result=(
            (
                type("Turn", (), {"sequence": 2, "actor_type": "assistant", "content": "hi"})(),
            ),
            False,
        )
    )
    client, _sessions = signed_in_client(
        hosted_web_conversation_enabled=True, web_conversation=conversation,
    )
    response = client.get(
        "/v1/conversation/turns?after=1", base_url=f"https://{HOST}"
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "schema_version": 1,
        "turns": [{"sequence": 2, "actor": "assistant", "text": "hi"}],
        "pending": False,
    }
    [(_, context, kwargs)] = conversation.calls
    assert context == TenantContext(TENANT_ID)
    assert kwargs == {"principal_id": PRINCIPAL_ID, "after": 1}


def test_web_conversation_turns_defaults_after_to_zero_and_reports_pending():
    conversation = WebConversation(turns_result=((), True))
    client, _sessions = signed_in_client(
        hosted_web_conversation_enabled=True, web_conversation=conversation,
    )
    response = client.get("/v1/conversation/turns", base_url=f"https://{HOST}")
    assert response.status_code == 200
    assert response.get_json()["pending"] is True
    [(_, _context, kwargs)] = conversation.calls
    assert kwargs["after"] == 0


@pytest.mark.parametrize("after", ["-1", "abc", "1.5", " 1", "9" * 25])
def test_web_conversation_turns_refuses_an_invalid_after_value(after):
    conversation = WebConversation()
    client, _sessions = signed_in_client(
        hosted_web_conversation_enabled=True, web_conversation=conversation,
    )
    response = client.get(
        f"/v1/conversation/turns?after={after}", base_url=f"https://{HOST}"
    )
    assert response.status_code == 400
    assert conversation.calls == []


def test_web_conversation_turns_requires_a_session():
    conversation = WebConversation()
    client = identity_client(
        Sessions(opened=False),
        hosted_web_conversation_enabled=True,
        web_conversation=conversation,
    )
    response = client.get("/v1/conversation/turns", base_url=f"https://{HOST}")
    assert response.status_code == 401
    assert conversation.calls == []


def test_web_conversation_requires_identity_and_a_service():
    with pytest.raises(ValueError, match="identity"):
        create_app(HOST, hosted_web_conversation_enabled=True)
    with pytest.raises(ValueError, match="identity"):
        create_app(
            HOST,
            identity_enabled=True,
            project_id=PROJECT,
            identity_api_key=API_KEY,
            identity_auth_domain=AUTH_DOMAIN,
            sessions=Sessions(),
            hosted_web_conversation_enabled=True,
            web_conversation=None,
        )
