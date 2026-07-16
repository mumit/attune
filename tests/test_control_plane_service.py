from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

pytest.importorskip("flask")

from attune.hosted.control_plane_service import create_app
from attune.hosted.identity import IdentityRefused, VerifiedIdentity
from attune.hosted.identity_session import IdentitySession
from attune.hosted.tenant import TenantContext

HOST = "dev.attune.mumit.org"
PROJECT = "attune-development-502421"
API_KEY = "AIza" + "a" * 35
AUTH_DOMAIN = f"{PROJECT}.firebaseapp.com"
WORKSPACE_CLIENT_ID = "123456789012-" + "a" * 32 + ".apps.googleusercontent.com"
TENANT_ID = UUID("10000000-0000-4000-8000-000000000001")
PRINCIPAL_ID = UUID("20000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("30000000-0000-4000-8000-000000000001")


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

    def revoke(self, token, csrf):
        self.calls.append(("revoke", token, csrf))
        return bool(self.session)


class OAuthStarts:
    def __init__(self, failure=None, connected=False):
        self.calls = []
        self.failure = failure
        self.connected = connected

    def start(self, context, **kwargs):
        self.calls.append((context, kwargs))
        if self.failure:
            raise self.failure

    def is_connected(self, context, *, principal_id):
        self.calls.append(("is_connected", context, principal_id))
        return self.connected


def verified(_token, project_id):
    assert project_id == PROJECT
    return VerifiedIdentity(
        issuer=f"https://securetoken.google.com/{PROJECT}",
        subject_hash=bytes.fromhex("11" * 32),
        authenticated_at=datetime.now(timezone.utc),
    )


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


def same_origin():
    return {"Origin": f"https://{HOST}", "Sec-Fetch-Site": "same-origin"}


def test_locked_shell_exposes_only_health_and_unavailable_root():
    client = create_app(HOST).test_client()
    headers = {"Host": HOST}
    health = client.get("/healthz", headers=headers)
    assert health.status_code == 200
    assert health.get_json() == {"status": "ok", "mode": "not_activated"}
    root = client.get("/", headers=headers)
    assert root.status_code == 503
    assert root.get_json() == {"status": "not_activated"}
    assert client.get("/oauth/google/callback", headers=headers).status_code == 404
    assert client.post("/", headers=headers).status_code == 405


def test_every_response_sets_strict_non_caching_browser_headers():
    response = create_app(HOST).test_client().get("/", headers={"Host": HOST})
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000"
    assert response.headers["Content-Security-Policy"] == (
        "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    )


def test_host_confusion_and_invalid_configuration_fail_closed():
    client = create_app(HOST).test_client()
    assert client.get("/healthz", headers={"Host": "evil.example"}).status_code == 400
    for value in ("https://dev.attune.mumit.org", "localhost", "DEV.example.com"):
        with pytest.raises(ValueError):
            create_app(value)


def test_identity_routes_do_not_exist_while_dormant():
    client = create_app(HOST).test_client()
    assert client.get("/v1/session/bootstrap", headers={"Host": HOST}).status_code == 404
    assert client.post("/v1/session", headers={"Host": HOST}).status_code == 404


def test_identity_ui_exposes_only_public_provider_configuration():
    client = identity_client()
    root = client.get("/", headers={"Host": HOST})
    assert root.status_code == 200
    assert "Continue with Google" in root.get_data(as_text=True)
    assert API_KEY not in root.get_data(as_text=True)
    config = client.get("/v1/identity/config", headers={"Host": HOST})
    assert config.get_json() == {
        "api_key": API_KEY,
        "auth_domain": AUTH_DOMAIN,
        "project_id": PROJECT,
    }
    assert "no-store" in config.headers["Cache-Control"]
    assert (
        "script-src 'self' https://apis.google.com"
        in root.headers["Content-Security-Policy"]
    )
    assert root.headers["Cross-Origin-Opener-Policy"] == ("same-origin-allow-popups")


@pytest.mark.parametrize(
    ("api_key", "auth_domain"),
    [
        (None, AUTH_DOMAIN),
        ("invalid", AUTH_DOMAIN),
        (API_KEY, "attacker.example"),
    ],
)
def test_identity_ui_rejects_inexact_public_provider_configuration(api_key, auth_domain):
    with pytest.raises(ValueError):
        create_app(
            HOST,
            identity_enabled=True,
            project_id=PROJECT,
            identity_api_key=api_key,
            identity_auth_domain=auth_domain,
            sessions=Sessions(),
        )


def test_identity_session_requires_same_origin_login_binding_and_membership():
    sessions = Sessions()
    client = identity_client(sessions)
    bootstrap = client.get("/v1/session/bootstrap", base_url=f"https://{HOST}")
    assert bootstrap.status_code == 200
    challenge = bootstrap.get_json()["login_challenge"]
    assert challenge not in repr(sessions.calls)
    refused = client.post(
        "/v1/session",
        json={"id_token": "signed", "login_challenge": challenge},
        base_url=f"https://{HOST}",
    )
    assert refused.status_code == 401
    opened = client.post(
        "/v1/session",
        json={"id_token": "signed", "login_challenge": challenge},
        headers=same_origin(),
        base_url=f"https://{HOST}",
    )
    assert opened.status_code == 200
    assert opened.get_json() == {"status": "authenticated"}
    cookies = opened.headers.getlist("Set-Cookie")
    assert any(
        "__Host-attune_session=" in value and "HttpOnly" in value for value in cookies
    )
    assert any(
        "__Host-attune_csrf=" in value and "SameSite=Strict" in value for value in cookies
    )
    assert all("signed" not in value for value in cookies)


def test_identity_session_refuses_invalid_token_and_ambiguous_membership():
    def refused(_token, _project):
        raise IdentityRefused("provider detail")

    for sessions, verifier, status in (
        (Sessions(), refused, 401),
        (Sessions(opened=False), verified, 409),
    ):
        client = identity_client(sessions, verifier)
        challenge = client.get(
            "/v1/session/bootstrap", base_url=f"https://{HOST}"
        ).get_json()["login_challenge"]
        response = client.post(
            "/v1/session",
            json={"id_token": "signed", "login_challenge": challenge},
            headers=same_origin(),
            base_url=f"https://{HOST}",
        )
        assert response.status_code == status
        assert "provider detail" not in response.get_data(as_text=True)


def test_session_read_and_signout_require_csrf():
    sessions = Sessions()
    client = identity_client(sessions)
    bootstrap = client.get("/v1/session/bootstrap", base_url=f"https://{HOST}").get_json()
    client.post(
        "/v1/session",
        json={"id_token": "signed", "login_challenge": bootstrap["login_challenge"]},
        headers=same_origin(),
        base_url=f"https://{HOST}",
    )
    assert client.get("/v1/session", base_url=f"https://{HOST}").status_code == 200
    assert (
        client.delete(
            "/v1/session", headers=same_origin(), base_url=f"https://{HOST}"
        ).status_code
        == 401
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    signed_out = client.delete(
        "/v1/session",
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert signed_out.status_code == 200
    assert [call[0] for call in sessions.calls][-2:] == ["authorize", "revoke"]


def test_google_workspace_start_is_authenticated_csrf_bound_and_canonical():
    sessions = Sessions()
    starts = OAuthStarts()
    client = identity_client(
        sessions,
        google_oauth_enabled=True,
        google_oauth_client_id=WORKSPACE_CLIENT_ID,
        google_oauth_starts=starts,
    )
    bootstrap = client.get("/v1/session/bootstrap", base_url=f"https://{HOST}").get_json()
    client.post(
        "/v1/session",
        json={"id_token": "signed", "login_challenge": bootstrap["login_challenge"]},
        headers=same_origin(),
        base_url=f"https://{HOST}",
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = client.post(
        "/v1/connectors/google/start",
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )

    assert response.status_code == 200
    authorization_url = response.get_json()["authorization_url"]
    assert authorization_url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert f"client_id={WORKSPACE_CLIENT_ID}" in authorization_url
    assert "response_type=code" in authorization_url
    assert "code_challenge_method=S256" in authorization_url
    assert "gmail.readonly" in authorization_url
    assert "calendar.readonly" in authorization_url
    assert len(starts.calls) == 1
    context, values = starts.calls[0]
    assert context == TenantContext(TENANT_ID)
    assert values["principal_id"] == PRINCIPAL_ID
    assert values["redirect_uri"] == f"https://{HOST}/oauth/google/callback"
    assert values["scopes"] == (
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
    )
    cookies = response.headers.getlist("Set-Cookie")
    assert any(
        "__Secure-attune_oauth_binding=" in value
        and "HttpOnly" in value
        and "Path=/oauth/google/callback" in value
        and "SameSite=Lax" in value
        for value in cookies
    )


def test_google_workspace_start_fails_closed_without_configuration_or_csrf():
    client = identity_client()
    assert (
        client.post(
            "/v1/connectors/google/start",
            headers=same_origin(),
            base_url=f"https://{HOST}",
        ).status_code
        == 503
    )
    with pytest.raises(ValueError):
        identity_client(
            google_oauth_enabled=True,
            google_oauth_client_id="invalid",
            google_oauth_starts=OAuthStarts(),
        )
