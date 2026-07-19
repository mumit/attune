from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

pytest.importorskip("flask")

from attune.hosted.control_plane_service import create_app
from attune.hosted.identity_session import IdentitySession
from attune.hosted.model_profile import TenantModelProfile
from attune.hosted.tenant import TenantContext

HOST = "dev.attune.mumit.org"
PROJECT = "attune-development-502421"
API_KEY = "AIza" + "a" * 35
AUTH_DOMAIN = f"{PROJECT}.firebaseapp.com"
TENANT_ID = UUID("10000000-0000-4000-8000-000000000021")
PRINCIPAL_ID = UUID("20000000-0000-4000-8000-000000000021")
SESSION_ID = UUID("30000000-0000-4000-8000-000000000021")


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


class ModelProfileService:
    def __init__(self, failure=None, current=None):
        self.calls = []
        self.failure = failure
        self.current = current

    def read(self, context):
        self.calls.append(("read", context))
        return self.current

    def configure(self, context, **kwargs):
        self.calls.append(("configure", context, kwargs))
        if self.failure:
            raise self.failure
        profile = kwargs["profile"]
        if not isinstance(profile, str) or profile not in {"standard", "premium"}:
            raise ValueError("model profile is invalid")
        return TenantModelProfile(1, profile, 2)


def verified(_token, project_id):
    from attune.hosted.identity import VerifiedIdentity

    assert project_id == PROJECT
    return VerifiedIdentity(
        issuer=f"https://securetoken.google.com/{PROJECT}",
        subject_hash=bytes.fromhex("33" * 32),
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


def test_model_profile_routes_are_absent_when_the_gate_is_off():
    """Gate-off pin: no route registered at all, matching every other gated
    ceremony's 404 behavior."""
    client, _sessions = signed_in_client()
    assert client.get("/v1/model-profile", base_url=f"https://{HOST}").status_code == 404
    assert client.put(
        "/v1/model-profile", json={"schema_version": 1, "profile": "standard"},
        base_url=f"https://{HOST}",
    ).status_code == 404


def test_model_profile_requires_identity_and_a_service():
    with pytest.raises(ValueError, match="identity"):
        create_app(HOST, hosted_model_profile_enabled=True)
    with pytest.raises(ValueError, match="identity"):
        create_app(
            HOST,
            identity_enabled=True,
            project_id=PROJECT,
            identity_api_key=API_KEY,
            identity_auth_domain=AUTH_DOMAIN,
            sessions=Sessions(),
            hosted_model_profile_enabled=True,
            hosted_model_profile=None,
        )


def test_read_defaults_to_standard_with_no_stored_preference():
    service = ModelProfileService(current=None)
    client, _sessions = signed_in_client(
        hosted_model_profile_enabled=True, hosted_model_profile=service,
    )
    response = client.get("/v1/model-profile", base_url=f"https://{HOST}")
    assert response.status_code == 200
    body = response.get_json()
    assert body["profile"] == "standard"
    assert body["revision"] == 0
    assert {"standard", "premium"} == {option["id"] for option in body["options"]}


def test_read_requires_a_session():
    service = ModelProfileService()
    client, sessions = signed_in_client(
        hosted_model_profile_enabled=True, hosted_model_profile=service,
    )
    sessions.session = None
    response = client.get("/v1/model-profile", base_url=f"https://{HOST}")
    assert response.status_code == 401


def test_configure_uses_ordinary_session_not_the_ten_minute_recency_window():
    """A bounded preference, not an authority change: the SAME ordinary
    session+CSRF bar as ``POST /v1/conversation/messages``/``POST
    /v1/brief/run`` -- no ``recent_authentication_required`` path exists for
    this route at all."""
    service = ModelProfileService()
    client, _sessions = signed_in_client(
        hosted_model_profile_enabled=True, hosted_model_profile=service,
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = client.put(
        "/v1/model-profile",
        json={"schema_version": 1, "profile": "premium"},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "schema_version": 1, "profile": "premium", "revision": 2,
        "options": [
            {"id": "standard", "label": "Standard"},
            {"id": "premium", "label": "Premium"},
        ],
    }
    [(_, context, kwargs)] = [call for call in service.calls if call[0] == "configure"]
    assert context == TenantContext(TENANT_ID)
    assert kwargs["principal_id"] == PRINCIPAL_ID
    assert kwargs["session_id"] == SESSION_ID
    assert kwargs["profile"] == "premium"


def test_configure_requires_origin_and_csrf_but_not_recency():
    service = ModelProfileService()
    client, _sessions = signed_in_client(
        hosted_model_profile_enabled=True, hosted_model_profile=service,
    )
    import werkzeug.test

    anonymous = werkzeug.test.Client(client.application)
    refused = anonymous.put(
        "/v1/model-profile", json={"schema_version": 1, "profile": "standard"},
        headers=same_origin(), base_url=f"https://{HOST}",
    )
    assert refused.status_code == 401
    assert service.calls == []

    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    cross_origin = client.put(
        "/v1/model-profile", json={"schema_version": 1, "profile": "standard"},
        headers={"Origin": "https://evil.example", "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert cross_origin.status_code == 401
    assert service.calls == []

    missing_csrf = client.put(
        "/v1/model-profile", json={"schema_version": 1, "profile": "standard"},
        headers=same_origin(), base_url=f"https://{HOST}",
    )
    assert missing_csrf.status_code == 401
    assert service.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"schema_version": 1},
        {"schema_version": 2, "profile": "standard"},
        {"schema_version": 1, "profile": "standard", "extra": True},
    ],
)
def test_configure_rejects_a_malformed_body_before_the_service_is_ever_called(body):
    service = ModelProfileService()
    client, _sessions = signed_in_client(
        hosted_model_profile_enabled=True, hosted_model_profile=service,
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = client.put(
        "/v1/model-profile", json=body,
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 400
    assert service.calls == []


@pytest.mark.parametrize(
    "profile",
    ["enterprise", 123, "", "Standard", "standard;drop"],
)
def test_configure_rejects_an_out_of_vocabulary_profile_validated_server_side(profile):
    """Vocabulary validation happens server-side (in the audited service/
    repository), not merely by trusting the browser's own <select> options."""
    service = ModelProfileService()
    client, _sessions = signed_in_client(
        hosted_model_profile_enabled=True, hosted_model_profile=service,
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = client.put(
        "/v1/model-profile", json={"schema_version": 1, "profile": profile},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_model_profile"}
    assert len(service.calls) == 1


def test_configure_failure_is_a_bounded_error():
    service = ModelProfileService(failure=RuntimeError("db unavailable, details"))
    client, _sessions = signed_in_client(
        hosted_model_profile_enabled=True, hosted_model_profile=service,
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = client.put(
        "/v1/model-profile", json={"schema_version": 1, "profile": "standard"},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 503
    assert response.get_json() == {"error": "model_profile_unavailable"}
    assert b"db unavailable" not in response.data


def test_session_status_reports_hosted_model_profile_availability():
    client, _sessions = signed_in_client(
        hosted_model_profile_enabled=True, hosted_model_profile=ModelProfileService(),
    )
    response = client.get("/v1/session", base_url=f"https://{HOST}")
    assert response.get_json()["hosted_model_profile"] == "available"

    client_off, _sessions = signed_in_client()
    response_off = client_off.get("/v1/session", base_url=f"https://{HOST}")
    assert response_off.get_json()["hosted_model_profile"] == "not_configured"
