from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

pytest.importorskip("flask")

from attune.hosted.control_plane_service import create_app
from attune.hosted.identity import (
    IdentityRefused,
    VerifiedIdentity,
    verify_identity_platform_token,
)
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


class CustomerExports:
    def __init__(self):
        self.calls = []
        now = datetime.now(timezone.utc)
        self.export = type(
            "Export",
            (),
            {
                "id": JOB_ID,
                "scope": "account",
                "state": "ready",
                "created_at": now,
                "updated_at": now,
                "ready_at": now,
                "expires_at": now + timedelta(hours=1),
                "archive_bytes": 123,
                "failure_code": None,
            },
        )()

    def list(self, context, **kwargs):
        self.calls.append(("list", context, kwargs))
        return (self.export,)

    def request(self, context, **kwargs):
        self.calls.append(("request", context, kwargs))
        return type("Started", (), {"export": self.export, "accepted": True})()

    def authorize_download(self, context, **kwargs):
        self.calls.append(("authorize", context, kwargs))
        return type(
            "Grant",
            (),
            {
                "id": UUID(int=99),
                "secret": "s" * 43,
                "expires_at": datetime.now(timezone.utc) + timedelta(seconds=90),
            },
        )()


class TenantDeletion:
    def __init__(self, existing=None, cancellable=True, failure=None):
        self.calls = []
        self.existing = existing
        self.cancellable = cancellable
        self.failure = failure

    def status(self, context, **kwargs):
        self.calls.append(("status", context, kwargs))
        if self.failure:
            raise self.failure
        return self.existing

    def request(self, context, **kwargs):
        self.calls.append(("request", context, kwargs))
        if self.failure:
            raise self.failure
        now = datetime.now(timezone.utc)
        return type(
            "Requested",
            (),
            {
                "id": JOB_ID,
                "status": "pending",
                "requested_at": now,
                "grace_expires_at": now + timedelta(days=14),
                "created": self.existing is None,
            },
        )()

    def cancel(self, context, **kwargs):
        self.calls.append(("cancel", context, kwargs))
        if self.failure:
            raise self.failure
        return type(
            "Cancelled",
            (),
            {
                "cancelled": self.cancellable,
                "status": "cancelled" if self.cancellable else "claimed",
            },
        )()


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


class ChannelSetup:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []
        self.states = (
            type(
                "ProviderState",
                (),
                {
                    "provider": "google_chat",
                    "selected": True,
                    "setup_state": "not_started",
                    "destination_state": "not_started",
                },
            )(),
            type(
                "ProviderState",
                (),
                {
                    "provider": "slack",
                    "selected": True,
                    "setup_state": "not_started",
                    "destination_state": "not_started",
                },
            )(),
        )

    def read(self, context, *, principal_id):
        self.calls.append(("read", context, principal_id))
        if self.failure:
            raise self.failure
        return self.states

    def begin(self, context, **kwargs):
        self.calls.append(("begin", context, kwargs))
        if self.failure:
            raise self.failure
        transaction = type(
            "Transaction",
            (),
            {
                "state": "pending",
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=9),
            },
        )()
        return type(
            "Started", (), {"transaction": transaction, "one_time_secret": "x" * 43}
        )()

    def complete_slack_install(self, context, **kwargs):
        self.calls.append(("install", context, kwargs))
        if self.failure:
            raise self.failure
        return True

    def test_delivery(self, context, **kwargs):
        self.calls.append(("test", context, kwargs))
        if self.failure:
            raise self.failure
        states = list(self.states)
        states[0] = type(
            "ProviderState",
            (),
            {
                "provider": "google_chat",
                "selected": True,
                "setup_state": "consumed",
                "destination_state": "active",
            },
        )()
        return tuple(states)

    def disconnect(self, context, **kwargs):
        self.calls.append(("disconnect", context, kwargs))
        if self.failure:
            raise self.failure
        states = list(self.states)
        states[0] = type(
            "ProviderState",
            (),
            {
                "provider": "google_chat",
                "selected": True,
                "setup_state": "consumed",
                "destination_state": "revoked",
            },
        )()
        self.states = tuple(states)
        return self.states


class Signup:
    def __init__(self, status="created", failure=None):
        self.status = status
        self.failure = failure
        self.calls = []

    def provision(self, identity):
        self.calls.append(identity)
        if self.failure:
            raise self.failure
        return type(
            "SignupResult",
            (),
            {
                "status": self.status,
                "tenant_id": TENANT_ID,
                "principal_id": PRINCIPAL_ID,
            },
        )()


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


def test_first_party_chat_avatar_is_public_on_the_exact_host():
    response = create_app(HOST).test_client().get(
        "/assets/attune-chat-avatar.png", headers={"Host": HOST}
    )
    assert response.status_code == 200
    assert response.content_type == "image/png"
    assert len(response.data) > 1_000


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
    page = root.get_data(as_text=True)
    assert "Continue with Google" in page
    assert API_KEY not in page
    # Hosted onboarding polish (Phase 6): first-run conversation hints (UX
    # review item #9) name only bounded executor routes (brief/Gmail/
    # Calendar/general) and never advertise a write.
    assert "What needs my attention today?" in page
    assert "Did anyone reply to the launch thread?" in page
    assert "What's on my calendar tomorrow?" in page
    # Web-panel reply notifications (deliverable 2): the opt-in control and
    # its denied/unsupported explanation text both render server-side; the
    # client decides at runtime whether to show the control or the text.
    assert "Notify me when Attune replies" in page
    # Recency-window pre-flight (UX review item #1): every route this page
    # enumerates as recency-gated in docs/hosted-policy.md/user-journey.md
    # carries a client-side hook the built bundle uses to mount its
    # advisory countdown/pre-flight banner.
    for gate in ("policy", "channels", "channel-installations", "deletion", "export"):
        assert f'data-recency-gate="{gate}"' in page
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


IDENTITY_CLAIMS_NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _identity_claims(**changes):
    value = {
        "iss": f"https://securetoken.google.com/{PROJECT}",
        "aud": PROJECT,
        "sub": "identity-platform-uid",
        "auth_time": int((IDENTITY_CLAIMS_NOW - timedelta(seconds=30)).timestamp()),
        "email_verified": True,
        "firebase": {"sign_in_provider": "google.com"},
    }
    value.update(changes)
    return value


def test_hosted_signup_routes_do_not_exist_while_the_gate_is_off():
    dormant = create_app(HOST).test_client()
    assert dormant.post("/v1/signup", headers={"Host": HOST}).status_code == 404

    client = identity_client()  # identity on, hosted_signup_enabled defaults False
    assert client.post("/v1/signup", headers={"Host": HOST}).status_code == 404


def test_hosted_signup_pins_the_unprovisioned_login_dead_end_unchanged():
    # The 409 dead-end from ordinary login must stay byte-identical whether
    # or not hosted signup exists on the same app instance.
    for kwargs in ({}, {"hosted_signup_enabled": True, "hosted_signup": Signup()}):
        client = identity_client(Sessions(opened=False), **kwargs)
        challenge = client.get(
            "/v1/session/bootstrap", base_url=f"https://{HOST}"
        ).get_json()["login_challenge"]
        response = client.post(
            "/v1/session",
            json={"id_token": "signed", "login_challenge": challenge},
            headers=same_origin(),
            base_url=f"https://{HOST}",
        )
        assert response.status_code == 409
        assert response.get_json() == {"error": "identity_membership_unavailable"}


def test_hosted_signup_requires_same_origin_and_login_binding():
    signup = Signup()
    client = identity_client(hosted_signup_enabled=True, hosted_signup=signup)
    bootstrap = client.get(
        "/v1/session/bootstrap", base_url=f"https://{HOST}"
    ).get_json()
    challenge = bootstrap["login_challenge"]

    cross_origin = client.post(
        "/v1/signup",
        json={"id_token": "signed", "login_challenge": challenge},
        base_url=f"https://{HOST}",
    )
    assert cross_origin.status_code == 401
    assert cross_origin.get_json() == {"error": "invalid_sign_in"}

    wrong_binding = client.post(
        "/v1/signup",
        json={"id_token": "signed", "login_challenge": "w" * 43},
        headers=same_origin(),
        base_url=f"https://{HOST}",
    )
    assert wrong_binding.status_code == 401
    assert signup.calls == []


def test_hosted_signup_rejects_any_payload_field_beyond_the_login_shape():
    # Structural proof that no free-form field -- a display name, a
    # requested slug -- can ever reach the provisioning service: the route
    # accepts only the exact {id_token, login_challenge} shape, identical to
    # POST /v1/session.
    signup = Signup()
    client = identity_client(hosted_signup_enabled=True, hosted_signup=signup)
    challenge = client.get(
        "/v1/session/bootstrap", base_url=f"https://{HOST}"
    ).get_json()["login_challenge"]
    response = client.post(
        "/v1/signup",
        json={
            "id_token": "signed",
            "login_challenge": challenge,
            "display_name": "attacker; DROP TABLE tenants;",
        },
        headers=same_origin(),
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 401
    assert signup.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"iss": "https://attacker.example"},
        {"aud": "another-project"},
        {"sub": ""},
        {"email_verified": False},
        {"firebase": {"sign_in_provider": "password"}},
        {"auth_time": int((IDENTITY_CLAIMS_NOW - timedelta(minutes=6)).timestamp())},
        {"auth_time": int((IDENTITY_CLAIMS_NOW + timedelta(minutes=1)).timestamp())},
    ],
)
def test_hosted_signup_reuses_the_login_verifier_and_rejects_identically(change):
    # Both routes call the exact same attune.hosted.identity function; a
    # token that login refuses must be refused by signup for the same
    # reason, using the same shared token_verifier hook.
    def low_level_verifier(_token, _project_id):
        return _identity_claims(**change)

    def shared_token_verifier(token, project_id):
        return verify_identity_platform_token(
            token, project_id, now=IDENTITY_CLAIMS_NOW, verifier=low_level_verifier
        )

    client = identity_client(
        Sessions(),
        verifier=shared_token_verifier,
        hosted_signup_enabled=True,
        hosted_signup=Signup(),
    )
    for path in ("/v1/session", "/v1/signup"):
        challenge = client.get(
            "/v1/session/bootstrap", base_url=f"https://{HOST}"
        ).get_json()["login_challenge"]
        response = client.post(
            path,
            json={"id_token": "signed", "login_challenge": challenge},
            headers=same_origin(),
            base_url=f"https://{HOST}",
        )
        assert response.status_code == 401
        assert response.get_json() == {"error": "invalid_sign_in"}


def test_hosted_signup_is_explicit_post_only_and_never_fires_on_mere_login():
    signup = Signup()
    client, _sessions = signed_in_client(
        hosted_signup_enabled=True, hosted_signup=signup
    )
    assert signup.calls == []
    assert client.get("/v1/signup", headers={"Host": HOST}).status_code == 405
    assert signup.calls == []


def test_hosted_signup_returns_created_or_already_provisioned():
    for status, code in (("created", 201), ("already_provisioned", 200)):
        signup = Signup(status=status)
        client = identity_client(hosted_signup_enabled=True, hosted_signup=signup)
        bootstrap = client.get(
            "/v1/session/bootstrap", base_url=f"https://{HOST}"
        ).get_json()
        response = client.post(
            "/v1/signup",
            json={"id_token": "signed", "login_challenge": bootstrap["login_challenge"]},
            headers=same_origin(),
            base_url=f"https://{HOST}",
        )
        assert response.status_code == code
        assert response.get_json() == {"status": status}
        assert len(signup.calls) == 1
        assert signup.calls[0].subject_hash == bytes.fromhex("11" * 32)
        # The one-use login-challenge cookie is cleared exactly like login's.
        cookies = "; ".join(response.headers.getlist("Set-Cookie"))
        assert "__Host-attune_login=" in cookies
        assert "Max-Age=0" in cookies


def test_hosted_signup_maps_unavailable_provisioning_to_a_generic_failure():
    signup = Signup(failure=RuntimeError("tenant conflict for slug xyz"))
    client = identity_client(hosted_signup_enabled=True, hosted_signup=signup)
    bootstrap = client.get(
        "/v1/session/bootstrap", base_url=f"https://{HOST}"
    ).get_json()
    response = client.post(
        "/v1/signup",
        json={"id_token": "signed", "login_challenge": bootstrap["login_challenge"]},
        headers=same_origin(),
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 503
    assert response.get_json() == {"error": "signup_unavailable"}
    assert "xyz" not in response.get_data(as_text=True)


def test_hosted_signup_throttles_repeated_attempts_from_the_same_subject():
    from attune.hosted.hosted_signup import SignupThrottle

    signup = Signup()
    throttle = SignupThrottle(limit=1, window=timedelta(minutes=5))
    client = identity_client(
        hosted_signup_enabled=True,
        hosted_signup=signup,
        hosted_signup_throttle=throttle,
    )
    for expected in (201, 429):
        bootstrap = client.get(
            "/v1/session/bootstrap", base_url=f"https://{HOST}"
        ).get_json()
        response = client.post(
            "/v1/signup",
            json={
                "id_token": "signed",
                "login_challenge": bootstrap["login_challenge"],
            },
            headers=same_origin(),
            base_url=f"https://{HOST}",
        )
        assert response.status_code == expected
    assert response.get_json() == {"error": "signup_throttled"}
    assert len(signup.calls) == 1


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


def test_customer_exports_are_owner_recent_bound_and_secret_is_returned_once():
    exports = CustomerExports()
    client, _sessions = signed_in_client(
        customer_exports_enabled=True, customer_exports=exports
    )
    listed = client.get("/v1/exports", base_url=f"https://{HOST}")
    assert listed.status_code == 200
    assert listed.get_json()["exports"][0]["download_available"] is True
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    headers = {**same_origin(), "X-Attune-CSRF": csrf}
    requested = client.post(
        "/v1/exports",
        json={"scope": "account", "confirmation": "create export"},
        headers=headers,
        base_url=f"https://{HOST}",
    )
    assert requested.status_code == 202
    issued = client.post(
        f"/v1/exports/{JOB_ID}/download-authorizations",
        json={"confirmation": "download export"},
        headers=headers,
        base_url=f"https://{HOST}",
    )
    assert issued.status_code == 201
    assert issued.get_json()["secret"] == "s" * 43
    assert [call[0] for call in exports.calls] == ["list", "request", "authorize"]


def test_customer_export_mutations_require_recent_auth_and_exact_body():
    exports = CustomerExports()
    client, _sessions = signed_in_client(
        sessions=Sessions(recent=False),
        customer_exports_enabled=True,
        customer_exports=exports,
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = client.post(
        "/v1/exports",
        json={"scope": "account", "confirmation": "create export"},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 409
    assert response.get_json() == {"error": "recent_authentication_required"}
    assert exports.calls == []


def test_customer_export_gate_off_pins_404():
    # customer_exports_enabled defaults to False; the routes must be absent
    # from the routing table entirely (plain 404), the same "unregistered,
    # not merely unauthenticated" pin every other default-off ceremony in
    # this codebase uses (hosted signup, tenant deletion).
    client = identity_client()
    assert (
        client.get("/v1/exports", base_url=f"https://{HOST}").status_code == 404
    )
    assert (
        client.post(
            "/v1/exports",
            json={"scope": "account", "confirmation": "create export"},
            base_url=f"https://{HOST}",
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/v1/exports/{JOB_ID}/download-authorizations",
            json={"confirmation": "download export"},
            base_url=f"https://{HOST}",
        ).status_code
        == 404
    )


def test_customer_exports_require_identity_when_enabled():
    with pytest.raises(ValueError, match="export"):
        identity_client(customer_exports_enabled=True)
    with pytest.raises(ValueError):
        create_app(
            HOST, customer_exports_enabled=True, customer_exports=CustomerExports()
        )


def test_tenant_deletion_gate_off_pins_404():
    client = identity_client()
    assert (
        client.get(
            "/v1/account/deletion-request", base_url=f"https://{HOST}"
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/v1/account/deletion-requests",
            json={"confirmation": "delete my account"},
            base_url=f"https://{HOST}",
        ).status_code
        == 404
    )
    assert (
        client.delete(
            "/v1/account/deletion-requests",
            json={"confirmation": "cancel deletion"},
            base_url=f"https://{HOST}",
        ).status_code
        == 404
    )


def test_tenant_deletion_requires_identity_when_enabled():
    with pytest.raises(ValueError, match="deletion"):
        identity_client(hosted_deletion_enabled=True)
    with pytest.raises(ValueError):
        create_app(HOST, hosted_deletion_enabled=True, hosted_deletion=TenantDeletion())


def test_tenant_deletion_status_reports_none_when_unrequested():
    deletion = TenantDeletion(existing=None)
    client, _sessions = signed_in_client(
        hosted_deletion_enabled=True, hosted_deletion=deletion
    )
    response = client.get("/v1/account/deletion-request", base_url=f"https://{HOST}")
    assert response.status_code == 200
    assert response.get_json() == {"schema_version": 1, "status": "none"}


def test_tenant_deletion_status_requires_a_session():
    deletion = TenantDeletion(existing=None)
    client = identity_client(
        sessions=Sessions(opened=False),
        hosted_deletion_enabled=True,
        hosted_deletion=deletion,
    )
    response = client.get("/v1/account/deletion-request", base_url=f"https://{HOST}")
    assert response.status_code == 401
    assert deletion.calls == []


def test_tenant_deletion_request_requires_recent_auth_csrf_and_exact_body():
    deletion = TenantDeletion()
    client, _sessions = signed_in_client(
        sessions=Sessions(recent=False),
        hosted_deletion_enabled=True,
        hosted_deletion=deletion,
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value

    # Wrong confirmation body is refused before any auth/CSRF check runs.
    wrong_body = client.post(
        "/v1/account/deletion-requests",
        json={"confirmation": "delete"},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert wrong_body.status_code == 400
    assert deletion.calls == []

    # Missing CSRF header is refused (invalid_session, not the recency 409).
    no_csrf = client.post(
        "/v1/account/deletion-requests",
        json={"confirmation": "delete my account"},
        headers=same_origin(),
        base_url=f"https://{HOST}",
    )
    assert no_csrf.status_code == 401
    assert deletion.calls == []

    # A valid but non-recent session gets the distinguishable 409.
    stale = client.post(
        "/v1/account/deletion-requests",
        json={"confirmation": "delete my account"},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert stale.status_code == 409
    assert stale.get_json() == {"error": "recent_authentication_required"}
    assert deletion.calls == []


def test_tenant_deletion_request_succeeds_with_recent_auth():
    deletion = TenantDeletion(existing=None)
    client, _sessions = signed_in_client(
        hosted_deletion_enabled=True, hosted_deletion=deletion
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = client.post(
        "/v1/account/deletion-requests",
        json={"confirmation": "delete my account"},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["deletion_request"]["status"] == "pending"
    assert deletion.calls[-1][0] == "request"
    assert deletion.calls[-1][2] == {
        "principal_id": PRINCIPAL_ID,
        "session_id": SESSION_ID,
    }


def test_tenant_deletion_cancel_requires_recent_auth_and_reports_conflict():
    deletion = TenantDeletion(cancellable=False)
    client, _sessions = signed_in_client(
        hosted_deletion_enabled=True, hosted_deletion=deletion
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = client.delete(
        "/v1/account/deletion-requests",
        json={"confirmation": "cancel deletion"},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 409
    assert response.get_json() == {"error": "deletion_not_cancellable"}

    invalid_body = client.delete(
        "/v1/account/deletion-requests",
        json={"confirmation": "nope"},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert invalid_body.status_code == 400


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


def test_hosted_channel_setup_is_recent_bound_and_returns_one_time_code():
    onboarding = Onboarding(OnboardingState())
    setup = ChannelSetup()
    client, _sessions = signed_in_client(
        hosted_onboarding_enabled=True,
        hosted_onboarding=onboarding,
        hosted_channels_enabled=True,
        hosted_channels=Channels(onboarding),
        hosted_channel_setup_enabled=True,
        hosted_channel_setup=setup,
    )
    review = client.get(
        "/v1/onboarding/channel-installations", base_url=f"https://{HOST}"
    )
    assert review.status_code == 200
    assert review.get_json() == {
        "schema_version": 1,
        "providers": [
            {
                "provider": "google_chat",
                "selected": True,
                "setup_state": "not_started",
                "destination_state": "not_started",
            },
            {
                "provider": "slack",
                "selected": True,
                "setup_state": "not_started",
                "destination_state": "not_started",
            },
        ],
        "destination_policy": "owner_dm_only",
        "test_delivery": "required",
    }
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    started = client.post(
        "/v1/onboarding/channel-installations/google-chat/link",
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert started.status_code == 201
    payload = started.get_json()
    assert payload["schema_version"] == 1
    assert payload["provider"] == "google_chat"
    assert payload["state"] == "pending"
    assert payload["link_command"] == "/link " + "x" * 43
    assert "transaction" not in payload
    assert setup.calls[-1][2]["principal_id"] == PRINCIPAL_ID
    assert setup.calls[-1][2]["session_id"] == SESSION_ID


def test_hosted_channel_setup_dependency_body_and_stale_auth_fail_closed():
    with pytest.raises(ValueError, match="channel setup"):
        identity_client(hosted_channel_setup_enabled=True)

    onboarding = Onboarding(OnboardingState())
    setup = ChannelSetup()
    client, _sessions = signed_in_client(
        sessions=Sessions(recent=False),
        hosted_onboarding_enabled=True,
        hosted_onboarding=onboarding,
        hosted_channels_enabled=True,
        hosted_channels=Channels(onboarding),
        hosted_channel_setup_enabled=True,
        hosted_channel_setup=setup,
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    path = "/v1/onboarding/channel-installations/google-chat/link"
    stale = client.post(
        path,
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert stale.status_code == 409
    assert stale.get_json() == {"error": "recent_authentication_required"}
    body = client.post(
        path,
        data=b"{}",
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert body.status_code == 400
    assert setup.calls == []


def test_hosted_google_chat_delivery_test_is_recent_bound_and_argument_free():
    onboarding = Onboarding(OnboardingState())
    setup = ChannelSetup()
    client, _sessions = signed_in_client(
        hosted_onboarding_enabled=True,
        hosted_onboarding=onboarding,
        hosted_channels_enabled=True,
        hosted_channels=Channels(onboarding),
        hosted_channel_setup_enabled=True,
        hosted_channel_setup=setup,
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    path = "/v1/onboarding/channel-installations/google-chat/test"
    assert client.post(
        path,
        json={"destination_id": "attacker"},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    ).status_code == 400
    response = client.post(
        path,
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 200
    assert response.get_json()["providers"][0]["destination_state"] == "active"
    assert setup.calls[-1][2] == {
        "principal_id": PRINCIPAL_ID,
        "session_id": SESSION_ID,
        "provider": "google_chat",
    }


def test_hosted_google_chat_disconnect_is_explicit_recent_and_principal_bound():
    onboarding = Onboarding(OnboardingState())
    setup = ChannelSetup()
    client, _sessions = signed_in_client(
        hosted_onboarding_enabled=True,
        hosted_onboarding=onboarding,
        hosted_channels_enabled=True,
        hosted_channels=Channels(onboarding),
        hosted_channel_setup_enabled=True,
        hosted_channel_setup=setup,
        hosted_channel_lifecycle_enabled=True,
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    path = "/v1/onboarding/channel-installations/google-chat"
    invalid = client.delete(
        path,
        json={"confirmation": "disconnect", "destination_id": "caller"},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert invalid.status_code == 400
    response = client.delete(
        path,
        json={"confirmation": "disconnect"},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 200
    assert response.get_json()["providers"][0]["destination_state"] == "revoked"
    assert setup.calls[-1] == (
        "disconnect",
        TenantContext(TENANT_ID),
        {
            "principal_id": PRINCIPAL_ID,
            "session_id": SESSION_ID,
            "provider": "google_chat",
        },
    )


def test_hosted_google_chat_disconnect_gate_and_recent_auth_fail_closed():
    with pytest.raises(ValueError, match="lifecycle"):
        identity_client(hosted_channel_lifecycle_enabled=True)

    onboarding = Onboarding(OnboardingState())
    setup = ChannelSetup()
    client, _sessions = signed_in_client(
        sessions=Sessions(recent=False),
        hosted_onboarding_enabled=True,
        hosted_onboarding=onboarding,
        hosted_channels_enabled=True,
        hosted_channels=Channels(onboarding),
        hosted_channel_setup_enabled=True,
        hosted_channel_setup=setup,
        hosted_channel_lifecycle_enabled=True,
    )
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    response = client.delete(
        "/v1/onboarding/channel-installations/google-chat",
        json={"confirmation": "disconnect"},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 409
    assert response.get_json() == {"error": "recent_authentication_required"}
    assert not any(call[0] == "disconnect" for call in setup.calls)


def slack_client(setup=None, sessions=None, lifecycle=False):
    onboarding = Onboarding(OnboardingState())
    kwargs = {
        "hosted_onboarding_enabled": True,
        "hosted_onboarding": onboarding,
        "hosted_channels_enabled": True,
        "hosted_channels": Channels(onboarding),
        "hosted_channel_setup_enabled": True,
        "hosted_channel_setup": setup or ChannelSetup(),
        "hosted_slack_install_enabled": True,
        "slack_client_id": "1234567890.0987654321",
    }
    if lifecycle:
        kwargs["hosted_channel_lifecycle_enabled"] = True
    if sessions is not None:
        kwargs["sessions"] = sessions
    return signed_in_client(**kwargs)


def test_hosted_slack_install_requires_channel_setup_and_public_client_id():
    with pytest.raises(ValueError, match="Slack"):
        identity_client(
            hosted_slack_install_enabled=True, slack_client_id="1234567890.1",
        )


def test_hosted_slack_install_begin_is_recent_bound_and_returns_authorize_url():
    setup = ChannelSetup()
    client, _sessions = slack_client(setup)
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    started = client.post(
        "/v1/onboarding/channel-installations/slack/install",
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert started.status_code == 201
    payload = started.get_json()
    assert payload["schema_version"] == 1
    assert payload["provider"] == "slack"
    assert payload["state"] == "pending"
    assert payload["authorize_url"].startswith(
        "https://slack.com/oauth/v2/authorize?"
    )
    assert "x" * 43 in payload["authorize_url"]
    assert setup.calls[-1][2]["provider"] == "slack"
    assert setup.calls[-1][2]["principal_id"] == PRINCIPAL_ID


def test_hosted_slack_callback_consumes_state_with_session_binding():
    setup = ChannelSetup()
    client, _sessions = slack_client(setup)
    response = client.get(
        "/v1/onboarding/channel-installations/slack/callback",
        query_string={"code": "code-123", "state": "x" * 43},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 303
    assert response.headers["Location"] == (
        f"https://{HOST}/?slack_install=connected"
    )
    call = next(call for call in setup.calls if call[0] == "install")
    assert call[2] == {
        "principal_id": PRINCIPAL_ID,
        "session_id": SESSION_ID,
        "state": "x" * 43,
        "code": "code-123",
    }


def test_hosted_slack_callback_fails_closed_without_session_or_state():
    setup = ChannelSetup()
    client, _sessions = slack_client(setup)
    denied = client.get(
        "/v1/onboarding/channel-installations/slack/callback",
        query_string={"code": "code-123", "state": "not-canonical"},
        base_url=f"https://{HOST}",
    )
    assert denied.status_code == 400
    provider_error = client.get(
        "/v1/onboarding/channel-installations/slack/callback",
        query_string={"error": "access_denied", "state": "x" * 43},
        base_url=f"https://{HOST}",
    )
    assert provider_error.status_code == 303
    assert provider_error.headers["Location"].endswith("slack_install=failed")
    extra = client.get(
        "/v1/onboarding/channel-installations/slack/callback",
        query_string={"code": "c", "state": "x" * 43, "tenant": "attacker"},
        base_url=f"https://{HOST}",
    )
    assert extra.status_code == 400
    assert not any(call[0] == "install" for call in setup.calls)

    fresh = identity_client(
        sessions=Sessions(opened=False),
        hosted_onboarding_enabled=True,
        hosted_onboarding=Onboarding(OnboardingState()),
        hosted_channels_enabled=True,
        hosted_channels=Channels(Onboarding(OnboardingState())),
        hosted_channel_setup_enabled=True,
        hosted_channel_setup=setup,
        hosted_slack_install_enabled=True,
        slack_client_id="1234567890.0987654321",
    )
    anonymous = fresh.get(
        "/v1/onboarding/channel-installations/slack/callback",
        query_string={"code": "code-123", "state": "x" * 43},
        base_url=f"https://{HOST}",
    )
    assert anonymous.status_code == 401


def test_hosted_slack_callback_failure_redirects_without_detail():
    setup = ChannelSetup(failure=RuntimeError("xoxb-secret"))
    client, _sessions = slack_client(setup)
    response = client.get(
        "/v1/onboarding/channel-installations/slack/callback",
        query_string={"code": "code-123", "state": "x" * 43},
        base_url=f"https://{HOST}",
    )
    assert response.status_code == 303
    assert response.headers["Location"].endswith("slack_install=failed")
    assert b"xoxb" not in response.data


def test_hosted_slack_delivery_test_and_disconnect_routes_pass_provider():
    setup = ChannelSetup()
    client, _sessions = slack_client(setup, lifecycle=True)
    csrf = client.get_cookie("__Host-attune_csrf", domain=HOST).value
    test = client.post(
        "/v1/onboarding/channel-installations/slack/test",
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert test.status_code == 200
    assert setup.calls[-1][2] == {
        "principal_id": PRINCIPAL_ID,
        "session_id": SESSION_ID,
        "provider": "slack",
    }
    disconnect = client.delete(
        "/v1/onboarding/channel-installations/slack",
        json={"confirmation": "disconnect"},
        headers={**same_origin(), "X-Attune-CSRF": csrf},
        base_url=f"https://{HOST}",
    )
    assert disconnect.status_code == 200
    assert setup.calls[-1][2]["provider"] == "slack"
