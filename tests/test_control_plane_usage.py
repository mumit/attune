from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID

import pytest

pytest.importorskip("flask")

from attune.hosted.control_plane_service import create_app
from attune.hosted.identity_session import IdentitySession
from attune.hosted.model_usage import DailyModelUsage
from attune.hosted.tenant import TenantContext

HOST = "dev.attune.mumit.org"
PROJECT = "attune-development-502421"
API_KEY = "AIza" + "a" * 35
AUTH_DOMAIN = f"{PROJECT}.firebaseapp.com"
TENANT_ID = UUID("10000000-0000-4000-8000-000000000031")
PRINCIPAL_ID = UUID("20000000-0000-4000-8000-000000000031")
SESSION_ID = UUID("30000000-0000-4000-8000-000000000031")


class Sessions:
    def __init__(self, opened=True):
        self.session = (
            IdentitySession(SESSION_ID, TenantContext(TENANT_ID), PRINCIPAL_ID)
            if opened
            else None
        )

    def open(self, identity, session_secrets, *, expires_at):
        return self.session

    def read(self, token):
        return self.session

    def authorize(self, token, csrf):
        return self.session

    def authorize_recent(self, token, csrf):
        return self.session

    def revoke(self, token, csrf):
        return bool(self.session)


class UsageService:
    def __init__(self, items=(), failure=None):
        self.items = items
        self.failure = failure
        self.calls = []

    def recent(self, context):
        self.calls.append(context)
        if self.failure:
            raise self.failure
        return list(self.items)


def verified(_token, project_id):
    from attune.hosted.identity import VerifiedIdentity

    assert project_id == PROJECT
    return VerifiedIdentity(
        issuer=f"https://securetoken.google.com/{PROJECT}",
        subject_hash=bytes.fromhex("44" * 32),
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


def test_usage_route_is_absent_when_the_gate_is_off():
    client, _sessions = signed_in_client()
    assert client.get("/v1/usage", base_url=f"https://{HOST}").status_code == 404


def test_usage_requires_identity_and_a_service():
    with pytest.raises(ValueError, match="identity"):
        create_app(HOST, hosted_usage_enabled=True)
    with pytest.raises(ValueError, match="identity"):
        create_app(
            HOST,
            identity_enabled=True,
            project_id=PROJECT,
            identity_api_key=API_KEY,
            identity_auth_domain=AUTH_DOMAIN,
            sessions=Sessions(),
            hosted_usage_enabled=True,
            hosted_usage=None,
        )


def test_usage_requires_a_session_no_csrf_needed_for_a_read():
    service = UsageService()
    client, sessions = signed_in_client(hosted_usage_enabled=True, hosted_usage=service)
    sessions.session = None
    assert client.get("/v1/usage", base_url=f"https://{HOST}").status_code == 401


def test_usage_returns_the_tenants_own_bounded_window():
    items = [
        DailyModelUsage(date(2026, 7, 19), "converse", "standard", 3, 100, 40, 0),
        DailyModelUsage(date(2026, 7, 18), "embed", "standard", 5, 20, 0, 1),
    ]
    service = UsageService(items=items)
    client, _sessions = signed_in_client(hosted_usage_enabled=True, hosted_usage=service)
    response = client.get("/v1/usage", base_url=f"https://{HOST}")
    assert response.status_code == 200
    body = response.get_json()
    assert body["schema_version"] == 1
    assert body["window_days"] == 30
    assert body["items"] == [
        {
            "date": "2026-07-19", "task": "converse", "profile": "standard",
            "request_count": 3, "input_tokens": 100, "output_tokens": 40,
            "failure_count": 0,
        },
        {
            "date": "2026-07-18", "task": "embed", "profile": "standard",
            "request_count": 5, "input_tokens": 20, "output_tokens": 0,
            "failure_count": 1,
        },
    ]
    assert service.calls == [TenantContext(TENANT_ID)]


def test_usage_returns_an_empty_list_when_nothing_has_been_recorded():
    service = UsageService(items=())
    client, _sessions = signed_in_client(hosted_usage_enabled=True, hosted_usage=service)
    response = client.get("/v1/usage", base_url=f"https://{HOST}")
    assert response.get_json()["items"] == []


def test_usage_failure_is_a_bounded_error():
    service = UsageService(failure=RuntimeError("db unavailable, details"))
    client, _sessions = signed_in_client(hosted_usage_enabled=True, hosted_usage=service)
    response = client.get("/v1/usage", base_url=f"https://{HOST}")
    assert response.status_code == 503
    assert response.get_json() == {"error": "usage_unavailable"}
    assert b"db unavailable" not in response.data


def test_session_status_reports_hosted_usage_availability():
    client, _sessions = signed_in_client(hosted_usage_enabled=True, hosted_usage=UsageService())
    assert client.get(
        "/v1/session", base_url=f"https://{HOST}"
    ).get_json()["hosted_usage"] == "available"

    client_off, _sessions = signed_in_client()
    assert client_off.get(
        "/v1/session", base_url=f"https://{HOST}"
    ).get_json()["hosted_usage"] == "not_configured"
