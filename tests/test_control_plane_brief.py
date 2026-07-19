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
TENANT_ID = UUID("10000000-0000-4000-8000-000000000011")
PRINCIPAL_ID = UUID("20000000-0000-4000-8000-000000000011")
SESSION_ID = UUID("30000000-0000-4000-8000-000000000011")
BRIEF_JOB_ID = UUID("40000000-0000-4000-8000-000000000011")


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


class HostedBrief:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def run(self, context, **kwargs):
        self.calls.append((context, kwargs))
        if self.failure:
            raise self.failure
        return type("Started", (), {"job_id": BRIEF_JOB_ID})()


def verified(_token, project_id):
    from attune.hosted.identity import VerifiedIdentity

    assert project_id == PROJECT
    return VerifiedIdentity(
        issuer=f"https://securetoken.google.com/{PROJECT}",
        subject_hash=bytes.fromhex("22" * 32),
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


def test_brief_route_is_absent_when_the_gate_is_off():
    """Gate-off pin (Phase 5 stage 4, G12): matches every other gated
    route's 404 behavior -- no route registered at all, not a disabled
    response."""
    client, _sessions = signed_in_client()
    response = client.post(
        "/v1/brief/run", json={"schema_version": 1}, base_url=f"https://{HOST}",
    )
    assert response.status_code == 404


def test_brief_run_requires_ordinary_session_origin_and_csrf():
    hosted_brief = HostedBrief()
    client, _sessions = signed_in_client(
        hosted_brief_enabled=True, hosted_brief=hosted_brief,
    )
    import werkzeug.test

    anonymous = werkzeug.test.Client(client.application)
    refused = anonymous.post(
        "/v1/brief/run", json={"schema_version": 1},
        headers=same_origin(), base_url=f"https://{HOST}",
    )
    assert refused.status_code == 401
    assert hosted_brief.calls == []

    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value

    cross_origin = client.post(
        "/v1/brief/run", json={"schema_version": 1},
        headers={"Origin": "https://evil.example", "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert cross_origin.status_code == 401
    assert hosted_brief.calls == []

    missing_csrf = client.post(
        "/v1/brief/run", json={"schema_version": 1},
        headers=same_origin(), base_url=f"https://{HOST}",
    )
    assert missing_csrf.status_code == 401
    assert hosted_brief.calls == []


def test_brief_run_accepts_and_dispatches():
    hosted_brief = HostedBrief()
    client, _sessions = signed_in_client(
        hosted_brief_enabled=True, hosted_brief=hosted_brief,
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = client.post(
        "/v1/brief/run", json={"schema_version": 1},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 202
    assert response.get_json() == {
        "schema_version": 1, "job_id": str(BRIEF_JOB_ID), "state": "accepted",
    }
    [(context, kwargs)] = hosted_brief.calls
    assert context == TenantContext(TENANT_ID)
    assert kwargs == {"principal_id": PRINCIPAL_ID}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"schema_version": 2},
        {"schema_version": 1, "extra": True},
        {"schema_version": "1"},
    ],
)
def test_brief_run_refuses_a_malformed_body(body):
    hosted_brief = HostedBrief()
    client, _sessions = signed_in_client(
        hosted_brief_enabled=True, hosted_brief=hosted_brief,
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = client.post(
        "/v1/brief/run", json=body,
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 400
    assert hosted_brief.calls == []


def test_brief_run_dispatch_failure_is_a_bounded_error():
    hosted_brief = HostedBrief(failure=RuntimeError("dispatch refused, details"))
    client, _sessions = signed_in_client(
        hosted_brief_enabled=True, hosted_brief=hosted_brief,
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = client.post(
        "/v1/brief/run", json={"schema_version": 1},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 503
    assert response.get_json() == {"error": "brief_unavailable"}
    assert b"dispatch refused" not in response.data


def test_brief_requires_identity_and_a_service():
    with pytest.raises(ValueError, match="identity"):
        create_app(HOST, hosted_brief_enabled=True)
    with pytest.raises(ValueError, match="identity"):
        create_app(
            HOST,
            identity_enabled=True,
            project_id=PROJECT,
            identity_api_key=API_KEY,
            identity_auth_domain=AUTH_DOMAIN,
            sessions=Sessions(),
            hosted_brief_enabled=True,
            hosted_brief=None,
        )


def test_session_status_reports_hosted_brief_availability():
    client, _sessions = signed_in_client(
        hosted_brief_enabled=True, hosted_brief=HostedBrief(),
    )
    response = client.get("/v1/session", base_url=f"https://{HOST}")
    assert response.get_json()["hosted_brief"] == "available"

    client_off, _sessions = signed_in_client()
    response_off = client_off.get("/v1/session", base_url=f"https://{HOST}")
    assert response_off.get_json()["hosted_brief"] == "not_configured"
