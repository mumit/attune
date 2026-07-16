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
JOB_ID = UUID("40000000-0000-4000-8000-000000000001")


class Sessions:
    def __init__(self, opened=True, recent=True):
        self.session = (
            IdentitySession(SESSION_ID, TenantContext(TENANT_ID), PRINCIPAL_ID)
            if opened
            else None
        )
        self.calls = []
        self.recent = recent

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
        return self.session if self.recent else None

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


class ConnectionTests:
    def __init__(self, started=True, state="succeeded", failure=None):
        self.started = started
        self.state = state
        self.failure = failure
        self.calls = []

    def start(self, context, *, principal_id):
        self.calls.append(("start", context, principal_id))
        if self.failure:
            raise self.failure
        if not self.started:
            return None
        return type("Started", (), {"job_id": JOB_ID, "state": "queued"})()

    def status(self, context, *, principal_id, job_id):
        self.calls.append(("status", context, principal_id, job_id))
        if self.failure:
            raise self.failure
        return self.state


class Revocations:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []

    def disconnect(self, context, *, principal_id):
        self.calls.append((context, principal_id))
        if self.failure:
            raise self.failure


class Onboarding:
    def __init__(self, state=None, failure=None):
        self.state = state
        self.failure = failure
        self.calls = []

    def read(self, context, *, principal_id):
        self.calls.append(("read", context, principal_id))
        if self.failure:
            raise self.failure
        return self.state

    def start(self, context, *, principal_id):
        self.calls.append(("start", context, principal_id))
        if self.failure:
            raise self.failure
        return self.state


class OnboardingState:
    schema_version = 1
    status = "in_progress"
    workspace = "validated"
    channels = "not_started"
    policy = "not_started"
    activation = "not_started"


class Policies:
    def __init__(self, onboarding, status="validated", failure=None):
        self.onboarding = onboarding
        self.status = status
        self.failure = failure
        self.calls = []

    def activate_read_only(self, context, *, principal_id, session_id):
        self.calls.append((context, principal_id, session_id))
        if self.failure:
            raise self.failure
        self.onboarding.state.policy = self.status
        return type("Activation", (), {"status": self.status})()


class Channels:
    def __init__(self, onboarding, preferences=None, failure=None):
        self.onboarding = onboarding
        self.preferences = preferences
        self.failure = failure
        self.calls = []

    def read(self, context, *, principal_id):
        self.calls.append(("read", context, principal_id))
        if self.failure:
            raise self.failure
        return self.preferences

    def configure(self, context, **kwargs):
        self.calls.append(("configure", context, kwargs))
        if self.failure:
            raise self.failure
        self.onboarding.state.channels = "authorized"
        self.preferences = type(
            "Preferences",
            (),
            {
                "interaction_channels": tuple(sorted(kwargs["interaction_channels"])),
                "brief_channels": tuple(sorted(kwargs["brief_channels"])),
            },
        )()
        return self.preferences


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


def signed_in_client(**kwargs):
    sessions = kwargs.pop("sessions", Sessions())
    client = identity_client(sessions, **kwargs)
    bootstrap = client.get("/v1/session/bootstrap", base_url=f"https://{HOST}").get_json()
    response = client.post(
        "/v1/session",
        json={"id_token": "signed", "login_challenge": bootstrap["login_challenge"]},
        headers=same_origin(),
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 200
    return client, sessions


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


def test_google_connection_test_is_session_csrf_and_principal_bound():
    tests = ConnectionTests()
    client, _sessions = signed_in_client(
        google_oauth_enabled=True,
        google_oauth_client_id=WORKSPACE_CLIENT_ID,
        google_oauth_starts=OAuthStarts(connected=True),
        google_connection_test_enabled=True,
        google_connection_tests=tests,
    )
    assert (
        client.post(
            "/v1/connectors/google/test",
            headers=same_origin(),
            base_url=f"https://{HOST}",
        ).status_code
        == 401
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    started = client.post(
        "/v1/connectors/google/test",
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert started.status_code == 202
    assert started.get_json() == {"job_id": str(JOB_ID), "state": "queued"}
    status = client.get(
        f"/v1/connectors/google/tests/{JOB_ID}", base_url=f"https://{HOST}"
    )
    assert status.get_json() == {"job_id": str(JOB_ID), "state": "succeeded"}
    assert tests.calls == [
        ("start", TenantContext(TENANT_ID), PRINCIPAL_ID),
        ("status", TenantContext(TENANT_ID), PRINCIPAL_ID, JOB_ID),
    ]


def test_google_connection_test_fails_closed_and_returns_only_opaque_state():
    for tests, expected in (
        (ConnectionTests(started=False), 409),
        (ConnectionTests(failure=RuntimeError("provider secret")), 503),
    ):
        client, _sessions = signed_in_client(
            google_oauth_enabled=True,
            google_oauth_client_id=WORKSPACE_CLIENT_ID,
            google_oauth_starts=OAuthStarts(connected=True),
            google_connection_test_enabled=True,
            google_connection_tests=tests,
        )
        csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
        response = client.post(
            "/v1/connectors/google/test",
            headers={**same_origin(), "X-Attune-CSRF": csrf},
            base_url=f"https://{HOST}",
        )
        assert response.status_code == expected
        assert b"provider secret" not in response.data

    client, _sessions = signed_in_client(
        google_oauth_enabled=True,
        google_oauth_client_id=WORKSPACE_CLIENT_ID,
        google_oauth_starts=OAuthStarts(connected=True),
        google_connection_test_enabled=True,
        google_connection_tests=ConnectionTests(state=None),
    )
    assert (
        client.get(
            f"/v1/connectors/google/tests/{JOB_ID}", base_url=f"https://{HOST}"
        ).status_code
        == 404
    )


def test_google_connection_test_configuration_fails_closed():
    with pytest.raises(ValueError, match="connection test"):
        identity_client(google_connection_test_enabled=True)


def test_google_disconnect_is_explicit_session_csrf_and_principal_bound():
    revocations = Revocations()
    client, _sessions = signed_in_client(
        google_oauth_enabled=True,
        google_oauth_client_id=WORKSPACE_CLIENT_ID,
        google_oauth_starts=OAuthStarts(connected=True),
        google_connector_revocation_enabled=True,
        google_connector_revocations=revocations,
    )
    url = "/v1/connectors/google"
    assert (
        client.delete(
            url,
            json={"confirmation": "disconnect"},
            base_url=f"https://{HOST}",
        ).status_code
        == 401
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    headers = {**same_origin(), "X-Attune-CSRF": csrf}
    assert (
        client.delete(
            url, json={}, headers=headers, base_url=f"https://{HOST}"
        ).status_code
        == 400
    )
    assert (
        client.delete(
            url,
            json={"confirmation": "disconnect", "connector_id": "caller"},
            headers=headers,
            base_url=f"https://{HOST}",
        ).status_code
        == 400
    )
    response = client.delete(
        url,
        json={"confirmation": "disconnect"},
        headers=headers,
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 200
    assert response.get_json() == {"status": "disconnected"}
    assert revocations.calls == [(TenantContext(TENANT_ID), PRINCIPAL_ID)]


def test_google_disconnect_configuration_and_failures_are_minimized():
    with pytest.raises(ValueError, match="revocation"):
        identity_client(google_connector_revocation_enabled=True)
    client, _sessions = signed_in_client(
        google_oauth_enabled=True,
        google_oauth_client_id=WORKSPACE_CLIENT_ID,
        google_oauth_starts=OAuthStarts(connected=True),
        google_connector_revocation_enabled=True,
        google_connector_revocations=Revocations(RuntimeError("provider secret")),
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = client.delete(
        "/v1/connectors/google",
        json={"confirmation": "disconnect"},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 503
    assert response.get_json() == {"error": "disconnect_unavailable"}
    assert b"provider secret" not in response.data


def test_google_connection_test_status_maps_invalid_session_before_availability():
    client = identity_client(
        Sessions(opened=False),
        google_oauth_enabled=True,
        google_oauth_client_id=WORKSPACE_CLIENT_ID,
        google_oauth_starts=OAuthStarts(),
        google_connection_test_enabled=True,
        google_connection_tests=ConnectionTests(
            failure=RuntimeError("must not run without a session")
        ),
    )
    response = client.get(
        f"/v1/connectors/google/tests/{JOB_ID}", base_url=f"https://{HOST}"
    )
    assert response.status_code == 401
    assert response.get_json() == {"error": "invalid_session"}


def test_hosted_onboarding_is_session_bound_explicit_and_minimized():
    onboarding = Onboarding(OnboardingState())
    client, _sessions = signed_in_client(
        hosted_onboarding_enabled=True, hosted_onboarding=onboarding
    )
    read = client.get("/v1/onboarding", base_url=f"https://{HOST}")
    assert read.get_json() == {
        "schema_version": 1,
        "status": "in_progress",
        "steps": {
            "workspace": "validated",
            "channels": "not_started",
            "policy": "not_started",
            "activation": "not_started",
        },
    }
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    assert (
        client.post(
            "/v1/onboarding/start",
            headers=same_origin(),
            base_url=f"https://{HOST}",
        ).status_code
        == 401
    )
    started = client.post(
        "/v1/onboarding/start",
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert started.status_code == 201
    assert onboarding.calls == [
        ("read", TenantContext(TENANT_ID), PRINCIPAL_ID),
        ("start", TenantContext(TENANT_ID), PRINCIPAL_ID),
    ]
    assert "tenant" not in started.get_json()
    assert "principal" not in started.get_json()


def test_hosted_onboarding_configuration_and_failure_are_closed():
    with pytest.raises(ValueError, match="onboarding"):
        identity_client(hosted_onboarding_enabled=True)
    client, _sessions = signed_in_client(
        hosted_onboarding_enabled=True,
        hosted_onboarding=Onboarding(failure=RuntimeError("private state")),
    )
    response = client.get("/v1/onboarding", base_url=f"https://{HOST}")
    assert response.status_code == 503
    assert response.get_json() == {"error": "onboarding_unavailable"}
    assert b"private state" not in response.data


def test_hosted_policy_is_reviewable_recent_auth_bound_and_fixed():
    onboarding = Onboarding(OnboardingState())
    policies = Policies(onboarding)
    client, _sessions = signed_in_client(
        hosted_onboarding_enabled=True,
        hosted_onboarding=onboarding,
        hosted_policy_enabled=True,
        hosted_policy=policies,
    )
    review = client.get("/v1/onboarding/policy", base_url=f"https://{HOST}")
    assert review.status_code == 200
    assert review.get_json() == {
        "schema_version": 1,
        "profile": "private_alpha_read_only",
        "status": "not_started",
        "maximum_risk": "R0",
        "automatic": ["Verify the read-only Gmail and Calendar connection"],
        "excluded": [
            "Send messages or email",
            "Change calendar events",
            "Delete or share provider data",
        ],
    }
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    assert (
        client.post(
            "/v1/onboarding/policy/confirm",
            headers={**same_origin(), "X-Attune-CSRF": csrf},
            data=b"not-empty",
            base_url=f"https://{HOST}",
        ).status_code
        == 400
    )
    confirmed = client.post(
        "/v1/onboarding/policy/confirm",
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert confirmed.status_code == 200
    assert confirmed.get_json()["policy"]["status"] == "validated"
    assert confirmed.get_json()["onboarding"]["steps"]["policy"] == "validated"
    assert policies.calls == [(TenantContext(TENANT_ID), PRINCIPAL_ID, SESSION_ID)]


def test_hosted_policy_requires_recent_auth_and_audited_service():
    with pytest.raises(ValueError, match="policy"):
        identity_client(hosted_policy_enabled=True)

    onboarding = Onboarding(OnboardingState())
    stale_sessions = Sessions(recent=False)
    client, _sessions = signed_in_client(
        sessions=stale_sessions,
        hosted_onboarding_enabled=True,
        hosted_onboarding=onboarding,
        hosted_policy_enabled=True,
        hosted_policy=Policies(onboarding),
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    stale = client.post(
        "/v1/onboarding/policy/confirm",
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert stale.status_code == 409
    assert stale.get_json() == {"error": "recent_authentication_required"}

    failed_policies = Policies(onboarding, failure=RuntimeError("private audit"))
    failed, _sessions = signed_in_client(
        hosted_onboarding_enabled=True,
        hosted_onboarding=onboarding,
        hosted_policy_enabled=True,
        hosted_policy=failed_policies,
    )
    csrf = failed.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = failed.post(
        "/v1/onboarding/policy/confirm",
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 503
    assert response.get_json() == {"error": "policy_unavailable"}
    assert b"private audit" not in response.data


def test_hosted_channels_are_bounded_recent_auth_and_effect_free():
    onboarding = Onboarding(OnboardingState())
    channels = Channels(onboarding)
    client, _sessions = signed_in_client(
        hosted_onboarding_enabled=True,
        hosted_onboarding=onboarding,
        hosted_channels_enabled=True,
        hosted_channels=channels,
    )
    review = client.get("/v1/onboarding/channels", base_url=f"https://{HOST}")
    assert review.status_code == 200
    assert review.get_json() == {
        "schema_version": 1,
        "status": "not_started",
        "interaction_channels": [],
        "brief_channels": [],
        "options": [
            {"id": "google_chat", "label": "Google Chat"},
            {"id": "slack", "label": "Slack"},
        ],
        "installation": "required",
    }
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    configured = client.put(
        "/v1/onboarding/channels",
        json={
            "schema_version": 1,
            "interaction_channels": ["slack", "google_chat"],
            "brief_channels": ["slack"],
        },
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert configured.status_code == 200
    assert configured.get_json()["channels"]["status"] == "authorized"
    assert configured.get_json()["channels"]["installation"] == "required"
    assert configured.get_json()["onboarding"]["steps"]["channels"] == "authorized"
    assert channels.calls[-1][2]["principal_id"] == PRINCIPAL_ID
    assert channels.calls[-1][2]["session_id"] == SESSION_ID


def test_hosted_channels_reject_invalid_or_stale_configuration():
    with pytest.raises(ValueError, match="channels"):
        identity_client(hosted_channels_enabled=True)
    onboarding = Onboarding(OnboardingState())
    client, _sessions = signed_in_client(
        sessions=Sessions(recent=False),
        hosted_onboarding_enabled=True,
        hosted_onboarding=onboarding,
        hosted_channels_enabled=True,
        hosted_channels=Channels(onboarding),
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    stale = client.put(
        "/v1/onboarding/channels",
        json={
            "schema_version": 1,
            "interaction_channels": ["slack"],
            "brief_channels": [],
        },
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert stale.status_code == 409
    assert stale.get_json() == {"error": "recent_authentication_required"}
