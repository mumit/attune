"""Locked hosted control plane with a disabled-by-default identity boundary."""

from __future__ import annotations

import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol
from urllib.parse import urlencode

from .hosted_signup import SignupResult, SignupThrottle
from .identity import IdentityRefused, VerifiedIdentity, verify_identity_platform_token
from .identity_session import (
    IdentitySession,
    IdentitySessionSecrets,
    create_identity_session_secrets,
)
from .oauth_transaction import create_oauth_transaction_secrets
from .slack_provider import build_authorize_url as build_slack_authorize_url

LOG = logging.getLogger(__name__)

HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
FIREBASE_API_KEY = re.compile(r"^AIza[0-9A-Za-z_-]{35}$")
LOGIN_COOKIE = "__Host-attune_login"
SESSION_COOKIE = "__Host-attune_session"
CSRF_COOKIE = "__Host-attune_csrf"
OAUTH_BINDING_COOKIE = "__Secure-attune_oauth_binding"
SESSION_LIFETIME = timedelta(hours=8)
OAUTH_LIFETIME = timedelta(minutes=10)
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_WORKSPACE_SCOPES = (
    "openid",
    "email",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
)
GOOGLE_CLIENT_ID = re.compile(
    r"^[0-9]{6,32}-[0-9A-Za-z_-]{16,96}\.apps\.googleusercontent\.com$"
)
SLACK_OAUTH_STATE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class SessionRepository(Protocol):
    def open(
        self,
        identity: VerifiedIdentity,
        session_secrets: IdentitySessionSecrets,
        *,
        expires_at: datetime,
    ) -> IdentitySession | None: ...

    def read(self, token: str) -> IdentitySession | None: ...

    def authorize(self, token: str, csrf: str) -> IdentitySession | None: ...

    def authorize_recent(self, token: str, csrf: str) -> IdentitySession | None: ...

    def revoke(self, token: str, csrf: str) -> bool: ...


class GoogleOAuthStartRepository(Protocol):
    def start(self, context, **kwargs): ...

    def is_connected(self, context, *, principal_id): ...


class GoogleConnectionTester(Protocol):
    def start(self, context, *, principal_id): ...

    def status(self, context, *, principal_id, job_id): ...


class GoogleConnectorRevoker(Protocol):
    def disconnect(self, context, *, principal_id) -> None: ...


class HostedOnboarding(Protocol):
    def read(self, context, *, principal_id): ...

    def start(self, context, *, principal_id): ...


class HostedPolicy(Protocol):
    def activate_read_only(self, context, *, principal_id, session_id): ...


class HostedChannels(Protocol):
    def read(self, context, *, principal_id): ...

    def configure(self, context, **kwargs): ...


class HostedChannelSetup(Protocol):
    def read(self, context, *, principal_id): ...

    def begin(self, context, **kwargs): ...

    def complete_slack_install(self, context, **kwargs): ...

    def test_delivery(self, context, **kwargs): ...

    def disconnect(self, context, **kwargs): ...


class CustomerExports(Protocol):
    def request(self, context, **kwargs): ...

    def list(self, context, **kwargs): ...

    def authorize_download(self, context, **kwargs): ...


class TenantDeletion(Protocol):
    def request(self, context, **kwargs): ...

    def cancel(self, context, **kwargs): ...

    def status(self, context, **kwargs): ...


class WebConversation(Protocol):
    def send(self, context, **kwargs): ...

    def turns(self, context, **kwargs): ...


class HostedBrief(Protocol):
    def run(self, context, **kwargs): ...


class HostedModelProfile(Protocol):
    def read(self, context): ...

    def configure(self, context, **kwargs): ...


class HostedUsage(Protocol):
    def recent(self, context): ...


class HostedSignup(Protocol):
    def provision(self, identity: VerifiedIdentity) -> SignupResult: ...


def create_app(
    expected_host: str,
    *,
    identity_enabled: bool = False,
    project_id: str | None = None,
    identity_api_key: str | None = None,
    identity_auth_domain: str | None = None,
    sessions: SessionRepository | None = None,
    google_oauth_enabled: bool = False,
    google_oauth_client_id: str | None = None,
    google_oauth_starts: GoogleOAuthStartRepository | None = None,
    google_connection_test_enabled: bool = False,
    google_connection_tests: GoogleConnectionTester | None = None,
    google_connector_revocation_enabled: bool = False,
    google_connector_revocations: GoogleConnectorRevoker | None = None,
    hosted_onboarding_enabled: bool = False,
    hosted_onboarding: HostedOnboarding | None = None,
    hosted_policy_enabled: bool = False,
    hosted_policy: HostedPolicy | None = None,
    hosted_channels_enabled: bool = False,
    hosted_channels: HostedChannels | None = None,
    hosted_channel_setup_enabled: bool = False,
    hosted_channel_setup: HostedChannelSetup | None = None,
    hosted_channel_lifecycle_enabled: bool = False,
    hosted_slack_install_enabled: bool = False,
    slack_client_id: str | None = None,
    customer_exports_enabled: bool = False,
    customer_exports: CustomerExports | None = None,
    hosted_deletion_enabled: bool = False,
    hosted_deletion: TenantDeletion | None = None,
    hosted_web_conversation_enabled: bool = False,
    web_conversation: WebConversation | None = None,
    hosted_brief_enabled: bool = False,
    hosted_brief: HostedBrief | None = None,
    hosted_model_profile_enabled: bool = False,
    hosted_model_profile: HostedModelProfile | None = None,
    hosted_usage_enabled: bool = False,
    hosted_usage: HostedUsage | None = None,
    hosted_signup_enabled: bool = False,
    hosted_signup: HostedSignup | None = None,
    hosted_signup_throttle: SignupThrottle | None = None,
    token_verifier: Callable[[str, str], VerifiedIdentity] = (
        verify_identity_platform_token
    ),
):
    from flask import Flask, Response, jsonify, render_template, request

    if not isinstance(expected_host, str) or not HOSTNAME.fullmatch(expected_host):
        raise ValueError("expected control-plane host must be a DNS hostname")
    if identity_enabled:
        expected_auth_domain = f"{project_id}.firebaseapp.com"
        if (
            not project_id
            or sessions is None
            or not isinstance(identity_api_key, str)
            or not FIREBASE_API_KEY.fullmatch(identity_api_key)
            or identity_auth_domain != expected_auth_domain
        ):
            raise ValueError(
                "enabled identity requires exact public provider configuration"
            )
    if google_oauth_enabled and (
        not identity_enabled
        or google_oauth_starts is None
        or not isinstance(google_oauth_client_id, str)
        or not GOOGLE_CLIENT_ID.fullmatch(google_oauth_client_id)
    ):
        raise ValueError(
            "enabled Google Workspace OAuth requires identity, a public client ID, "
            "and a transaction repository"
        )
    if google_connection_test_enabled and (
        not google_oauth_enabled or google_connection_tests is None
    ):
        raise ValueError(
            "enabled Google connection test requires Google Workspace OAuth "
            "and a fixed test service"
        )
    if google_connector_revocation_enabled and (
        not google_oauth_enabled or google_connector_revocations is None
    ):
        raise ValueError(
            "enabled Google connector revocation requires Google Workspace OAuth "
            "and a fixed revocation service"
        )
    if hosted_onboarding_enabled and (not identity_enabled or hosted_onboarding is None):
        raise ValueError(
            "enabled hosted onboarding requires identity and a tenant-bound repository"
        )
    if hosted_policy_enabled and (not hosted_onboarding_enabled or hosted_policy is None):
        raise ValueError(
            "enabled hosted policy requires hosted onboarding and an audited policy service"
        )
    if hosted_channels_enabled and (
        not hosted_onboarding_enabled or hosted_channels is None
    ):
        raise ValueError(
            "enabled hosted channels require hosted onboarding and an audited service"
        )
    if hosted_channel_setup_enabled and (
        not hosted_channels_enabled or hosted_channel_setup is None
    ):
        raise ValueError(
            "enabled hosted channel setup requires channel preferences and an "
            "audited setup service"
        )
    if hosted_channel_lifecycle_enabled and not hosted_channel_setup_enabled:
        raise ValueError(
            "enabled hosted channel lifecycle requires hosted channel setup"
        )
    if hosted_slack_install_enabled and (
        not hosted_channel_setup_enabled
        or not isinstance(slack_client_id, str)
        or not 1 <= len(slack_client_id) <= 64
    ):
        raise ValueError(
            "enabled hosted Slack installation requires hosted channel setup "
            "and a public Slack client ID"
        )
    if customer_exports_enabled and (
        not identity_enabled or customer_exports is None
    ):
        raise ValueError(
            "enabled customer exports require identity and an export service"
        )
    if hosted_deletion_enabled and (not identity_enabled or hosted_deletion is None):
        raise ValueError(
            "enabled hosted deletion requires identity and an audited service"
        )
    if hosted_web_conversation_enabled and (
        not identity_enabled or web_conversation is None
    ):
        raise ValueError(
            "enabled web conversation requires identity and an audited service"
        )
    if hosted_brief_enabled and (not identity_enabled or hosted_brief is None):
        raise ValueError(
            "enabled hosted brief requires identity and a dispatching service"
        )
    if hosted_model_profile_enabled and (
        not identity_enabled or hosted_model_profile is None
    ):
        raise ValueError(
            "enabled hosted model profile requires identity and an audited service"
        )
    if hosted_usage_enabled and (not identity_enabled or hosted_usage is None):
        raise ValueError(
            "enabled hosted usage requires identity and a usage service"
        )
    if hosted_signup_enabled and (not identity_enabled or hosted_signup is None):
        raise ValueError(
            "enabled hosted signup requires identity and a provisioning service"
        )
    signup_throttle = hosted_signup_throttle
    if hosted_signup_enabled and signup_throttle is None:
        signup_throttle = SignupThrottle()
    app = Flask(__name__, static_url_path="/assets")
    app.config.update(
        MAX_CONTENT_LENGTH=20_000 if identity_enabled else 1024,
        TRUSTED_HOSTS=[expected_host],
    )
    expected_origin = f"https://{expected_host}"

    @app.after_request
    def security_headers(response: Response):
        response.headers["Cache-Control"] = "no-store"
        if identity_enabled:
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self' https://apis.google.com; "
                "style-src 'self'; connect-src 'self' "
                "https://identitytoolkit.googleapis.com "
                "https://securetoken.googleapis.com; frame-src "
                f"https://{identity_auth_domain} https://accounts.google.com; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
            )
            response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                "form-action 'none'"
            )
            response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/healthz")
    def health():
        mode = "identity_staged" if identity_enabled else "not_activated"
        return jsonify({"status": "ok", "mode": mode})

    @app.get("/")
    def unavailable():
        if identity_enabled:
            return render_template("sign_in.html")
        return jsonify({"status": "not_activated"}), 503

    if identity_enabled:

        @app.get("/v1/identity/config")
        def identity_config():
            return jsonify(
                {
                    "api_key": identity_api_key,
                    "auth_domain": identity_auth_domain,
                    "project_id": project_id,
                }
            )

        @app.get("/v1/session/bootstrap")
        def session_bootstrap():
            challenge = secrets.token_urlsafe(32)
            response = jsonify({"login_challenge": challenge})
            response.set_cookie(
                LOGIN_COOKIE,
                challenge,
                max_age=300,
                secure=True,
                httponly=True,
                samesite="Lax",
                path="/",
            )
            return response

        @app.post("/v1/session")
        def open_session():
            if not _same_origin_request(request, expected_origin) or not request.is_json:
                return jsonify({"error": "invalid_sign_in"}), 401
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict) or set(payload) != {
                "id_token",
                "login_challenge",
            }:
                return jsonify({"error": "invalid_sign_in"}), 401
            token = payload["id_token"]
            challenge = payload["login_challenge"]
            cookie_challenge = request.cookies.get(LOGIN_COOKIE, "")
            if (
                not isinstance(token, str)
                or not isinstance(challenge, str)
                or len(challenge) != 43
                or not hmac.compare_digest(challenge, cookie_challenge)
            ):
                return jsonify({"error": "invalid_sign_in"}), 401
            try:
                identity = token_verifier(token, project_id)  # type: ignore[arg-type]
                session_secrets = create_identity_session_secrets()
                opened = sessions.open(  # type: ignore[union-attr]
                    identity,
                    session_secrets,
                    expires_at=datetime.now(timezone.utc) + SESSION_LIFETIME,
                )
            except IdentityRefused:
                return jsonify({"error": "invalid_sign_in"}), 401
            except Exception:
                return jsonify({"error": "sign_in_unavailable"}), 503
            if opened is None:
                return jsonify({"error": "identity_membership_unavailable"}), 409
            response = jsonify({"status": "authenticated"})
            response.delete_cookie(LOGIN_COOKIE, path="/", secure=True, samesite="Lax")
            response.set_cookie(
                SESSION_COOKIE,
                session_secrets.token,
                max_age=int(SESSION_LIFETIME.total_seconds()),
                secure=True,
                httponly=True,
                samesite="Lax",
                path="/",
            )
            response.set_cookie(
                CSRF_COOKIE,
                session_secrets.csrf,
                max_age=int(SESSION_LIFETIME.total_seconds()),
                secure=True,
                httponly=False,
                samesite="Strict",
                path="/",
            )
            return response

        if hosted_signup_enabled:

            @app.post("/v1/signup")
            def hosted_signup_provision():
                # Same shape and same anti-CSRF login binding as
                # POST /v1/session (see docs/hosted-signup.md section 2) --
                # signup has no session to authorize a mutation with, so it
                # reuses login's own same-origin + login-challenge proof
                # rather than inventing a second one.
                if (
                    not _same_origin_request(request, expected_origin)
                    or not request.is_json
                ):
                    return jsonify({"error": "invalid_sign_in"}), 401
                payload = request.get_json(silent=True)
                if not isinstance(payload, dict) or set(payload) != {
                    "id_token",
                    "login_challenge",
                }:
                    return jsonify({"error": "invalid_sign_in"}), 401
                token = payload["id_token"]
                challenge = payload["login_challenge"]
                cookie_challenge = request.cookies.get(LOGIN_COOKIE, "")
                if (
                    not isinstance(token, str)
                    or not isinstance(challenge, str)
                    or len(challenge) != 43
                    or not hmac.compare_digest(challenge, cookie_challenge)
                ):
                    return jsonify({"error": "invalid_sign_in"}), 401
                now = datetime.now(timezone.utc)
                client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                if client_ip and not signup_throttle.allow(  # type: ignore[union-attr]
                    b"ip:" + client_ip.encode("utf-8", "ignore"), now=now
                ):
                    LOG.info("hosted_signup_throttled scope=ip")
                    return jsonify({"error": "signup_throttled"}), 429
                try:
                    identity = token_verifier(token, project_id)  # type: ignore[arg-type]
                except IdentityRefused:
                    return jsonify({"error": "invalid_sign_in"}), 401
                except Exception:
                    return jsonify({"error": "sign_in_unavailable"}), 503
                if not signup_throttle.allow(  # type: ignore[union-attr]
                    b"subject:" + identity.subject_hash, now=now
                ):
                    LOG.info("hosted_signup_throttled scope=subject")
                    return jsonify({"error": "signup_throttled"}), 429
                LOG.info("hosted_signup_attempted")
                try:
                    result = hosted_signup.provision(identity)  # type: ignore[union-attr]
                except Exception:
                    return jsonify({"error": "signup_unavailable"}), 503
                response = jsonify({"status": result.status})
                response.delete_cookie(
                    LOGIN_COOKIE, path="/", secure=True, samesite="Lax"
                )
                response.status_code = 201 if result.status == "created" else 200
                return response

        @app.get("/v1/session")
        def read_session():
            token = request.cookies.get(SESSION_COOKIE, "")
            try:
                session = sessions.read(token)  # type: ignore[union-attr]
            except Exception:
                session = None
            if session is None:
                return jsonify({"authenticated": False}), 401
            google_workspace_oauth = "not_configured"
            if google_oauth_enabled:
                try:
                    google_workspace_oauth = (
                        "connected"
                        if google_oauth_starts.is_connected(  # type: ignore[union-attr]
                            session.context, principal_id=session.principal_id
                        )
                        else "available"
                    )
                except Exception:
                    google_workspace_oauth = "unavailable"
            return jsonify(
                {
                    "authenticated": True,
                    "google_workspace_oauth": google_workspace_oauth,
                    "hosted_onboarding": (
                        "available" if hosted_onboarding_enabled else "not_configured"
                    ),
                    "hosted_policy": (
                        "available" if hosted_policy_enabled else "not_configured"
                    ),
                    "hosted_channels": (
                        "available" if hosted_channels_enabled else "not_configured"
                    ),
                    "hosted_channel_setup": (
                        "available"
                        if hosted_channel_setup_enabled
                        else "not_configured"
                    ),
                    "hosted_channel_lifecycle": (
                        "available"
                        if hosted_channel_lifecycle_enabled
                        else "not_configured"
                    ),
                    "hosted_slack_install": (
                        "available"
                        if hosted_slack_install_enabled
                        else "not_configured"
                    ),
                    "customer_exports": (
                        "available" if customer_exports_enabled else "not_configured"
                    ),
                    "hosted_web_conversation": (
                        "available"
                        if hosted_web_conversation_enabled
                        else "not_configured"
                    ),
                    "hosted_brief": (
                        "available" if hosted_brief_enabled else "not_configured"
                    ),
                    "hosted_model_profile": (
                        "available" if hosted_model_profile_enabled else "not_configured"
                    ),
                    "hosted_usage": (
                        "available" if hosted_usage_enabled else "not_configured"
                    ),
                }
            )

        @app.post("/v1/connectors/google/start")
        def start_google_connector():
            if not google_oauth_enabled:
                return jsonify({"error": "connector_not_configured"}), 503
            if not _same_origin_request(request, expected_origin):
                return jsonify({"error": "invalid_session"}), 401
            token = request.cookies.get(SESSION_COOKIE, "")
            csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
            csrf_header = request.headers.get("X-Attune-CSRF", "")
            if not csrf_cookie or not hmac.compare_digest(csrf_cookie, csrf_header):
                return jsonify({"error": "invalid_session"}), 401
            try:
                authorized = sessions.authorize(  # type: ignore[union-attr]
                    token, csrf_cookie
                )
                if authorized is None:
                    return jsonify({"error": "invalid_session"}), 401
                transaction = create_oauth_transaction_secrets()
                redirect_uri = f"{expected_origin}/oauth/google/callback"
                google_oauth_starts.start(  # type: ignore[union-attr]
                    authorized.context,
                    principal_id=authorized.principal_id,
                    state_hash=transaction.state_hash,
                    binding_hash=transaction.binding_hash,
                    nonce_hash=transaction.nonce_hash,
                    pkce_verifier=transaction.pkce_verifier,
                    redirect_uri=redirect_uri,
                    scopes=GOOGLE_WORKSPACE_SCOPES,
                    expires_at=datetime.now(timezone.utc) + OAUTH_LIFETIME,
                )
            except RuntimeError:
                return jsonify({"error": "connector_already_active"}), 409
            except Exception:
                return jsonify({"error": "connector_unavailable"}), 503
            authorization_url = (
                GOOGLE_AUTHORIZATION_ENDPOINT
                + "?"
                + urlencode(
                    {
                        "client_id": google_oauth_client_id,
                        "redirect_uri": redirect_uri,
                        "response_type": "code",
                        "scope": " ".join(GOOGLE_WORKSPACE_SCOPES),
                        "access_type": "offline",
                        "include_granted_scopes": "false",
                        "prompt": "consent select_account",
                        "state": transaction.state,
                        "nonce": transaction.nonce,
                        "code_challenge": transaction.pkce_challenge,
                        "code_challenge_method": "S256",
                    }
                )
            )
            response = jsonify({"authorization_url": authorization_url})
            response.set_cookie(
                OAUTH_BINDING_COOKIE,
                transaction.binding,
                max_age=int(OAUTH_LIFETIME.total_seconds()),
                secure=True,
                httponly=True,
                samesite="Lax",
                path="/oauth/google/callback",
            )
            return response

        @app.post("/v1/connectors/google/test")
        def test_google_connector():
            if not google_connection_test_enabled:
                return jsonify({"error": "test_not_configured"}), 503
            if request.content_length not in {None, 0}:
                return jsonify({"error": "invalid_request"}), 400
            authorized = _authorize_mutation(
                request,
                expected_origin,
                sessions,  # type: ignore[arg-type]
            )
            if authorized is None:
                return jsonify({"error": "invalid_session"}), 401
            try:
                started = google_connection_tests.start(  # type: ignore[union-attr]
                    authorized.context,
                    principal_id=authorized.principal_id,
                )
            except Exception:
                return jsonify({"error": "test_unavailable"}), 503
            if started is None:
                return jsonify({"error": "connector_not_connected"}), 409
            return jsonify({"job_id": str(started.job_id), "state": started.state}), 202

        @app.delete("/v1/connectors/google")
        def disconnect_google_connector():
            if not google_connector_revocation_enabled:
                return jsonify({"error": "disconnect_not_configured"}), 503
            if not request.is_json:
                return jsonify({"error": "invalid_request"}), 400
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict) or payload != {"confirmation": "disconnect"}:
                return jsonify({"error": "invalid_request"}), 400
            authorized = _authorize_mutation(
                request,
                expected_origin,
                sessions,  # type: ignore[arg-type]
            )
            if authorized is None:
                return jsonify({"error": "invalid_session"}), 401
            try:
                google_connector_revocations.disconnect(  # type: ignore[union-attr]
                    authorized.context,
                    principal_id=authorized.principal_id,
                )
            except Exception:
                return jsonify({"error": "disconnect_unavailable"}), 503
            return jsonify({"status": "disconnected"})

        @app.get("/v1/connectors/google/tests/<uuid:job_id>")
        def google_connector_test_status(job_id):
            token = request.cookies.get(SESSION_COOKIE, "")
            try:
                session = sessions.read(token)  # type: ignore[union-attr]
            except Exception:
                session = None
            if session is None:
                return jsonify({"error": "invalid_session"}), 401
            try:
                state = (
                    google_connection_tests.status(  # type: ignore[union-attr]
                        session.context,
                        principal_id=session.principal_id,
                        job_id=job_id,
                    )
                    if google_connection_test_enabled
                    else None
                )
            except Exception:
                return jsonify({"error": "test_unavailable"}), 503
            if state is None:
                return jsonify({"error": "test_not_found"}), 404
            return jsonify({"job_id": str(job_id), "state": state})

        @app.delete("/v1/session")
        def delete_session():
            if not _same_origin_request(request, expected_origin):
                return jsonify({"error": "invalid_session"}), 401
            token = request.cookies.get(SESSION_COOKIE, "")
            csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
            csrf_header = request.headers.get("X-Attune-CSRF", "")
            if not csrf_cookie or not hmac.compare_digest(csrf_cookie, csrf_header):
                return jsonify({"error": "invalid_session"}), 401
            try:
                authorized = sessions.authorize(  # type: ignore[union-attr]
                    token, csrf_cookie
                )
                revoked = bool(
                    authorized and sessions.revoke(token, csrf_cookie)  # type: ignore[union-attr]
                )
            except Exception:
                return jsonify({"error": "session_unavailable"}), 503
            if not revoked:
                return jsonify({"error": "invalid_session"}), 401
            response = jsonify({"status": "signed_out"})
            response.delete_cookie(SESSION_COOKIE, path="/", secure=True)
            response.delete_cookie(CSRF_COOKIE, path="/", secure=True)
            return response

        @app.get("/v1/onboarding")
        def read_hosted_onboarding():
            if not hosted_onboarding_enabled:
                return jsonify({"error": "onboarding_not_configured"}), 503
            session = _read_session(request, sessions)  # type: ignore[arg-type]
            if session is None:
                return jsonify({"error": "invalid_session"}), 401
            try:
                state = hosted_onboarding.read(  # type: ignore[union-attr]
                    session.context, principal_id=session.principal_id
                )
            except Exception:
                return jsonify({"error": "onboarding_unavailable"}), 503
            return jsonify(_public_onboarding(state))

        @app.post("/v1/onboarding/start")
        def start_hosted_onboarding():
            if not hosted_onboarding_enabled:
                return jsonify({"error": "onboarding_not_configured"}), 503
            if request.content_length not in {None, 0}:
                return jsonify({"error": "invalid_request"}), 400
            session = _authorize_mutation(
                request,
                expected_origin,
                sessions,  # type: ignore[arg-type]
            )
            if session is None:
                return jsonify({"error": "invalid_session"}), 401
            try:
                state = hosted_onboarding.start(  # type: ignore[union-attr]
                    session.context, principal_id=session.principal_id
                )
            except Exception:
                return jsonify({"error": "onboarding_unavailable"}), 503
            return jsonify(_public_onboarding(state)), 201

        if hosted_policy_enabled:

            @app.get("/v1/onboarding/policy")
            def read_hosted_policy():
                session = _read_session(request, sessions)  # type: ignore[arg-type]
                if session is None:
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    state = hosted_onboarding.read(  # type: ignore[union-attr]
                        session.context, principal_id=session.principal_id
                    )
                except Exception:
                    return jsonify({"error": "policy_unavailable"}), 503
                if state is None:
                    return jsonify({"error": "onboarding_not_started"}), 409
                return jsonify(_public_policy(state.policy))

            @app.post("/v1/onboarding/policy/confirm")
            def confirm_hosted_policy():
                if request.content_length not in {None, 0}:
                    return jsonify({"error": "invalid_request"}), 400
                session = _authorize_mutation(
                    request,
                    expected_origin,
                    sessions,  # type: ignore[arg-type]
                    recent=True,
                )
                if session is None:
                    current = _authorize_mutation(
                        request,
                        expected_origin,
                        sessions,  # type: ignore[arg-type]
                    )
                    if current is not None:
                        return jsonify({"error": "recent_authentication_required"}), 409
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    result = hosted_policy.activate_read_only(  # type: ignore[union-attr]
                        session.context,
                        principal_id=session.principal_id,
                        session_id=session.id,
                    )
                    state = hosted_onboarding.read(  # type: ignore[union-attr]
                        session.context, principal_id=session.principal_id
                    )
                except Exception:
                    return jsonify({"error": "policy_unavailable"}), 503
                if result.status != "validated" or state is None:
                    return jsonify({"error": "policy_requires_repair"}), 409
                return jsonify(
                    {
                        "policy": _public_policy(result.status),
                        "onboarding": _public_onboarding(state),
                    }
                )

        if customer_exports_enabled:

            @app.get("/v1/exports")
            def list_customer_exports():
                session = _read_session(request, sessions)  # type: ignore[arg-type]
                if session is None:
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    exports = customer_exports.list(  # type: ignore[union-attr]
                        session.context, principal_id=session.principal_id
                    )
                except Exception:
                    return jsonify({"error": "exports_unavailable"}), 503
                return jsonify(
                    {
                        "schema_version": 1,
                        "exports": [_public_export(item) for item in exports],
                    }
                )

            @app.post("/v1/exports")
            def request_customer_export():
                if not request.is_json:
                    return jsonify({"error": "invalid_request"}), 400
                payload = request.get_json(silent=True)
                if not isinstance(payload, dict) or payload != {
                    "scope": "account",
                    "confirmation": "create export",
                }:
                    return jsonify({"error": "invalid_request"}), 400
                session = _authorize_mutation(
                    request,
                    expected_origin,
                    sessions,  # type: ignore[arg-type]
                    recent=True,
                )
                if session is None:
                    current = _authorize_mutation(
                        request,
                        expected_origin,
                        sessions,  # type: ignore[arg-type]
                    )
                    if current is not None:
                        return jsonify(
                            {"error": "recent_authentication_required"}
                        ), 409
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    started = customer_exports.request(  # type: ignore[union-attr]
                        session.context,
                        principal_id=session.principal_id,
                        session_id=session.id,
                        scope="account",
                    )
                except Exception:
                    return jsonify({"error": "export_unavailable"}), 503
                return (
                    jsonify(
                        {
                            "schema_version": 1,
                            "export": _public_export(started.export),
                        }
                    ),
                    202 if started.accepted else 200,
                )

            @app.post("/v1/exports/<uuid:export_id>/download-authorizations")
            def authorize_customer_export_download(export_id):
                if not request.is_json:
                    return jsonify({"error": "invalid_request"}), 400
                payload = request.get_json(silent=True)
                if not isinstance(payload, dict) or payload != {
                    "confirmation": "download export"
                }:
                    return jsonify({"error": "invalid_request"}), 400
                session = _authorize_mutation(
                    request,
                    expected_origin,
                    sessions,  # type: ignore[arg-type]
                    recent=True,
                )
                if session is None:
                    current = _authorize_mutation(
                        request,
                        expected_origin,
                        sessions,  # type: ignore[arg-type]
                    )
                    if current is not None:
                        return jsonify(
                            {"error": "recent_authentication_required"}
                        ), 409
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    grant = customer_exports.authorize_download(  # type: ignore[union-attr]
                        session.context,
                        principal_id=session.principal_id,
                        session_id=session.id,
                        export_id=export_id,
                    )
                except Exception:
                    return jsonify({"error": "download_not_available"}), 409
                return jsonify(
                    {
                        "schema_version": 1,
                        "grant_id": str(grant.id),
                        "secret": grant.secret,
                        "expires_at": grant.expires_at.isoformat(),
                    }
                ), 201

        if hosted_deletion_enabled:

            @app.get("/v1/account/deletion-request")
            def read_tenant_deletion_request():
                session = _read_session(request, sessions)  # type: ignore[arg-type]
                if session is None:
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    state = hosted_deletion.status(  # type: ignore[union-attr]
                        session.context, principal_id=session.principal_id
                    )
                except Exception:
                    return jsonify({"error": "deletion_unavailable"}), 503
                return jsonify(_public_deletion_request(state))

            @app.post("/v1/account/deletion-requests")
            def request_tenant_deletion_route():
                if not request.is_json:
                    return jsonify({"error": "invalid_request"}), 400
                payload = request.get_json(silent=True)
                if not isinstance(payload, dict) or payload != {
                    "confirmation": "delete my account"
                }:
                    return jsonify({"error": "invalid_request"}), 400
                session = _authorize_mutation(
                    request,
                    expected_origin,
                    sessions,  # type: ignore[arg-type]
                    recent=True,
                )
                if session is None:
                    current = _authorize_mutation(
                        request,
                        expected_origin,
                        sessions,  # type: ignore[arg-type]
                    )
                    if current is not None:
                        return jsonify(
                            {"error": "recent_authentication_required"}
                        ), 409
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    result = hosted_deletion.request(  # type: ignore[union-attr]
                        session.context,
                        principal_id=session.principal_id,
                        session_id=session.id,
                    )
                except Exception:
                    return jsonify({"error": "deletion_unavailable"}), 503
                return (
                    jsonify(
                        {
                            "schema_version": 1,
                            "deletion_request": {
                                "id": str(result.id),
                                "status": result.status,
                                "requested_at": result.requested_at.isoformat(),
                                "grace_expires_at": (
                                    result.grace_expires_at.isoformat()
                                ),
                            },
                        }
                    ),
                    201 if result.created else 200,
                )

            @app.delete("/v1/account/deletion-requests")
            def cancel_tenant_deletion_route():
                if not request.is_json:
                    return jsonify({"error": "invalid_request"}), 400
                payload = request.get_json(silent=True)
                if not isinstance(payload, dict) or payload != {
                    "confirmation": "cancel deletion"
                }:
                    return jsonify({"error": "invalid_request"}), 400
                session = _authorize_mutation(
                    request,
                    expected_origin,
                    sessions,  # type: ignore[arg-type]
                    recent=True,
                )
                if session is None:
                    current = _authorize_mutation(
                        request,
                        expected_origin,
                        sessions,  # type: ignore[arg-type]
                    )
                    if current is not None:
                        return jsonify(
                            {"error": "recent_authentication_required"}
                        ), 409
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    result = hosted_deletion.cancel(  # type: ignore[union-attr]
                        session.context,
                        principal_id=session.principal_id,
                        session_id=session.id,
                    )
                except Exception:
                    return jsonify({"error": "deletion_unavailable"}), 503
                if not result.cancelled:
                    return jsonify({"error": "deletion_not_cancellable"}), 409
                return jsonify({"schema_version": 1, "status": result.status})

        if hosted_web_conversation_enabled:

            @app.post("/v1/conversation/messages")
            def send_web_conversation_message():
                if not request.is_json:
                    return jsonify({"error": "invalid_request"}), 400
                payload = request.get_json(silent=True)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"schema_version", "text"}
                    or payload.get("schema_version") != 1
                    or not isinstance(payload.get("text"), str)
                    or not 1 <= len(payload["text"]) <= 8_000
                ):
                    return jsonify({"error": "invalid_request"}), 400
                session = _authorize_mutation(
                    request, expected_origin, sessions,  # type: ignore[arg-type]
                )
                if session is None:
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    accepted = web_conversation.send(  # type: ignore[union-attr]
                        session.context,
                        principal_id=session.principal_id,
                        session_id=session.id,
                        text=payload["text"],
                    )
                except Exception:
                    return jsonify({"error": "conversation_unavailable"}), 503
                return jsonify(
                    {
                        "schema_version": 1,
                        "conversation": str(accepted.conversation_id),
                        "user_sequence": accepted.user_sequence,
                        "state": "accepted",
                    }
                ), 202

            @app.get("/v1/conversation/turns")
            def read_web_conversation_turns():
                session = _read_session(request, sessions)  # type: ignore[arg-type]
                if session is None:
                    return jsonify({"error": "invalid_session"}), 401
                raw_after = request.args.get("after", "0")
                try:
                    after = int(raw_after)
                    if str(after) != raw_after or not 0 <= after < 2**63:
                        raise ValueError("out of bounds")
                except ValueError:
                    return jsonify({"error": "invalid_request"}), 400
                try:
                    turns, pending = web_conversation.turns(  # type: ignore[union-attr]
                        session.context,
                        principal_id=session.principal_id,
                        after=after,
                    )
                except Exception:
                    return jsonify({"error": "conversation_unavailable"}), 503
                return jsonify(
                    {
                        "schema_version": 1,
                        "turns": [
                            {
                                "sequence": turn.sequence,
                                "actor": turn.actor_type,
                                "text": turn.content,
                            }
                            for turn in turns
                        ],
                        "pending": pending,
                    }
                )

        if hosted_brief_enabled:

            @app.post("/v1/brief/run")
            def run_hosted_brief():
                """Create the brief job + dispatch intent (Deliverable 2,
                docs/future-state.md Phase 5 item 4). Ordinary session,
                same-origin, and CSRF proofs -- not the ten-minute recency
                reserved for destructive ceremonies -- the same bar as
                ``POST /v1/conversation/messages`` (see the dated
                'Web conversation acceptance uses ordinary proofs, not
                recency' entry in decisions.md: triggering a bounded,
                read-only-executed job is not an authority-changing
                ceremony). Idempotent per tenant per principal per UTC hour
                (``HostedBriefProducer``'s documented bound) -- a second
                click in the same hour returns the same job, never a
                duplicate delivery."""
                if not request.is_json:
                    return jsonify({"error": "invalid_request"}), 400
                payload = request.get_json(silent=True)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"schema_version"}
                    or payload.get("schema_version") != 1
                ):
                    return jsonify({"error": "invalid_request"}), 400
                session = _authorize_mutation(
                    request, expected_origin, sessions,  # type: ignore[arg-type]
                )
                if session is None:
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    started = hosted_brief.run(  # type: ignore[union-attr]
                        session.context, principal_id=session.principal_id,
                    )
                except Exception:
                    return jsonify({"error": "brief_unavailable"}), 503
                return jsonify(
                    {
                        "schema_version": 1,
                        "job_id": str(started.job_id),
                        "state": "accepted",
                    }
                ), 202

        if hosted_model_profile_enabled:

            @app.get("/v1/model-profile")
            def read_model_profile():
                session = _read_session(request, sessions)  # type: ignore[arg-type]
                if session is None:
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    current = hosted_model_profile.read(  # type: ignore[union-attr]
                        session.context
                    )
                except Exception:
                    return jsonify({"error": "model_profile_unavailable"}), 503
                return jsonify(_public_model_profile(current))

            @app.put("/v1/model-profile")
            def configure_model_profile():
                """A bounded owner preference, not an authority change --
                ordinary session, same-origin, and CSRF proofs, the same bar
                as ``POST /v1/conversation/messages``/``POST /v1/brief/run``
                (see the dated 'Web conversation acceptance uses ordinary
                proofs, not recency' entry in decisions.md), not the
                ten-minute recency window ``PUT /v1/onboarding/channels``
                reserves for a channel-authority change."""
                if not request.is_json:
                    return jsonify({"error": "invalid_request"}), 400
                payload = request.get_json(silent=True)
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"schema_version", "profile"}
                    or payload.get("schema_version") != 1
                ):
                    return jsonify({"error": "invalid_request"}), 400
                session = _authorize_mutation(
                    request, expected_origin, sessions,  # type: ignore[arg-type]
                )
                if session is None:
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    result = hosted_model_profile.configure(  # type: ignore[union-attr]
                        session.context,
                        principal_id=session.principal_id,
                        session_id=session.id,
                        profile=payload["profile"],
                    )
                except (TypeError, ValueError):
                    return jsonify({"error": "invalid_model_profile"}), 400
                except Exception:
                    return jsonify({"error": "model_profile_unavailable"}), 503
                return jsonify(_public_model_profile(result))

        if hosted_usage_enabled:

            @app.get("/v1/usage")
            def read_model_usage():
                """The customer-facing half of metering: the tenant's own
                bounded 30-day daily aggregates, content-free by
                construction. Ordinary session, no CSRF needed (a read)."""
                session = _read_session(request, sessions)  # type: ignore[arg-type]
                if session is None:
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    items = hosted_usage.recent(session.context)  # type: ignore[union-attr]
                except Exception:
                    return jsonify({"error": "usage_unavailable"}), 503
                return jsonify(_public_usage(items))

        if hosted_channels_enabled:

            @app.get("/v1/onboarding/channels")
            def read_hosted_channels():
                session = _read_session(request, sessions)  # type: ignore[arg-type]
                if session is None:
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    state = hosted_onboarding.read(  # type: ignore[union-attr]
                        session.context, principal_id=session.principal_id
                    )
                    preferences = hosted_channels.read(  # type: ignore[union-attr]
                        session.context, principal_id=session.principal_id
                    )
                except Exception:
                    return jsonify({"error": "channels_unavailable"}), 503
                if state is None:
                    return jsonify({"error": "onboarding_not_started"}), 409
                return jsonify(_public_channels(preferences, state.channels))

            @app.put("/v1/onboarding/channels")
            def configure_hosted_channels():
                if not request.is_json:
                    return jsonify({"error": "invalid_request"}), 400
                payload = request.get_json(silent=True)
                if (
                    not isinstance(payload, dict)
                    or set(payload)
                    != {
                        "schema_version",
                        "interaction_channels",
                        "brief_channels",
                    }
                    or payload.get("schema_version") != 1
                ):
                    return jsonify({"error": "invalid_request"}), 400
                session = _authorize_mutation(
                    request,
                    expected_origin,
                    sessions,  # type: ignore[arg-type]
                    recent=True,
                )
                if session is None:
                    current = _authorize_mutation(
                        request,
                        expected_origin,
                        sessions,  # type: ignore[arg-type]
                    )
                    if current is not None:
                        return jsonify({"error": "recent_authentication_required"}), 409
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    preferences = hosted_channels.configure(  # type: ignore[union-attr]
                        session.context,
                        principal_id=session.principal_id,
                        session_id=session.id,
                        interaction_channels=payload["interaction_channels"],
                        brief_channels=payload["brief_channels"],
                    )
                    state = hosted_onboarding.read(  # type: ignore[union-attr]
                        session.context, principal_id=session.principal_id
                    )
                except (TypeError, ValueError):
                    return jsonify({"error": "invalid_channel_preferences"}), 400
                except Exception:
                    return jsonify({"error": "channels_unavailable"}), 503
                if state is None:
                    return jsonify({"error": "onboarding_not_started"}), 409
                return jsonify(
                    {
                        "channels": _public_channels(preferences, state.channels),
                        "onboarding": _public_onboarding(state),
                    }
                )

        if hosted_channel_setup_enabled:

            @app.get("/v1/onboarding/channel-installations")
            def read_hosted_channel_installations():
                session = _read_session(request, sessions)  # type: ignore[arg-type]
                if session is None:
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    states = hosted_channel_setup.read(  # type: ignore[union-attr]
                        session.context, principal_id=session.principal_id
                    )
                except Exception:
                    return jsonify({"error": "channel_setup_unavailable"}), 503
                return jsonify(_public_channel_installations(states))

            @app.post("/v1/onboarding/channel-installations/google-chat/link")
            def begin_google_chat_link():
                if request.content_length not in {None, 0}:
                    return jsonify({"error": "invalid_request"}), 400
                session = _authorize_mutation(
                    request,
                    expected_origin,
                    sessions,  # type: ignore[arg-type]
                    recent=True,
                )
                if session is None:
                    current = _authorize_mutation(
                        request,
                        expected_origin,
                        sessions,  # type: ignore[arg-type]
                    )
                    if current is not None:
                        return jsonify({"error": "recent_authentication_required"}), 409
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    started = hosted_channel_setup.begin(  # type: ignore[union-attr]
                        session.context,
                        principal_id=session.principal_id,
                        session_id=session.id,
                        provider="google_chat",
                    )
                except ValueError:
                    return jsonify({"error": "invalid_channel_setup"}), 400
                except Exception:
                    return jsonify({"error": "channel_setup_unavailable"}), 503
                return (
                    jsonify(
                        {
                            "schema_version": 1,
                            "provider": "google_chat",
                            "state": started.transaction.state,
                            "link_command": f"/link {started.one_time_secret}",
                            "expires_at": started.transaction.expires_at.isoformat(),
                        }
                    ),
                    201,
                )

            @app.post("/v1/onboarding/channel-installations/google-chat/test")
            def test_google_chat_delivery():
                if request.content_length not in {None, 0}:
                    return jsonify({"error": "invalid_request"}), 400
                session = _authorize_mutation(
                    request,
                    expected_origin,
                    sessions,  # type: ignore[arg-type]
                    recent=True,
                )
                if session is None:
                    current = _authorize_mutation(
                        request, expected_origin, sessions  # type: ignore[arg-type]
                    )
                    if current is not None:
                        return jsonify({"error": "recent_authentication_required"}), 409
                    return jsonify({"error": "invalid_session"}), 401
                try:
                    states = hosted_channel_setup.test_delivery(  # type: ignore[union-attr]
                        session.context,
                        principal_id=session.principal_id,
                        session_id=session.id,
                        provider="google_chat",
                    )
                except ValueError:
                    return jsonify({"error": "invalid_channel_setup"}), 400
                except Exception:
                    return jsonify({"error": "channel_delivery_unavailable"}), 503
                return jsonify(_public_channel_installations(states))

            if hosted_slack_install_enabled:

                @app.post("/v1/onboarding/channel-installations/slack/install")
                def begin_slack_install():
                    if request.content_length not in {None, 0}:
                        return jsonify({"error": "invalid_request"}), 400
                    session = _authorize_mutation(
                        request,
                        expected_origin,
                        sessions,  # type: ignore[arg-type]
                        recent=True,
                    )
                    if session is None:
                        current = _authorize_mutation(
                            request,
                            expected_origin,
                            sessions,  # type: ignore[arg-type]
                        )
                        if current is not None:
                            return jsonify(
                                {"error": "recent_authentication_required"}
                            ), 409
                        return jsonify({"error": "invalid_session"}), 401
                    try:
                        started = hosted_channel_setup.begin(  # type: ignore[union-attr]
                            session.context,
                            principal_id=session.principal_id,
                            session_id=session.id,
                            provider="slack",
                        )
                        authorize_url = build_slack_authorize_url(
                            client_id=slack_client_id,  # type: ignore[arg-type]
                            state=started.one_time_secret,
                            redirect_uri=(
                                f"{expected_origin}"
                                "/v1/onboarding/channel-installations/slack/callback"
                            ),
                        )
                    except ValueError:
                        return jsonify({"error": "invalid_channel_setup"}), 400
                    except Exception:
                        return jsonify({"error": "channel_setup_unavailable"}), 503
                    return (
                        jsonify(
                            {
                                "schema_version": 1,
                                "provider": "slack",
                                "state": started.transaction.state,
                                "authorize_url": authorize_url,
                                "expires_at": (
                                    started.transaction.expires_at.isoformat()
                                ),
                            }
                        ),
                        201,
                    )

                @app.get("/v1/onboarding/channel-installations/slack/callback")
                def complete_slack_install_callback():
                    # Slack redirects the owner's browser here; this is a
                    # top-level cross-site navigation, so origin and CSRF
                    # headers cannot exist. The one-use state and the session
                    # cookie are the binding, and the private broker's
                    # database function rechecks both against the setup
                    # transaction before any mutation.
                    session = _read_session(request, sessions)  # type: ignore[arg-type]
                    if session is None:
                        return jsonify({"error": "invalid_session"}), 401
                    if set(request.args) - {"code", "state", "error"} != set():
                        return jsonify({"error": "invalid_request"}), 400
                    state = request.args.get("state", "")
                    code = request.args.get("code", "")
                    provider_error = request.args.get("error")
                    failure = Response(status=303)
                    failure.headers["Location"] = f"{expected_origin}/?slack_install=failed"
                    if provider_error is not None or not code:
                        return failure
                    if not SLACK_OAUTH_STATE.fullmatch(state) or not 1 <= len(code) <= 512:
                        return jsonify({"error": "invalid_request"}), 400
                    try:
                        installed = hosted_channel_setup.complete_slack_install(  # type: ignore[union-attr]
                            session.context,
                            principal_id=session.principal_id,
                            session_id=session.id,
                            state=state,
                            code=code,
                        )
                    except Exception:
                        installed = False
                    if not installed:
                        return failure
                    success = Response(status=303)
                    success.headers["Location"] = (
                        f"{expected_origin}/?slack_install=connected"
                    )
                    return success

                @app.post("/v1/onboarding/channel-installations/slack/test")
                def test_slack_delivery_route():
                    if request.content_length not in {None, 0}:
                        return jsonify({"error": "invalid_request"}), 400
                    session = _authorize_mutation(
                        request,
                        expected_origin,
                        sessions,  # type: ignore[arg-type]
                        recent=True,
                    )
                    if session is None:
                        current = _authorize_mutation(
                            request, expected_origin, sessions  # type: ignore[arg-type]
                        )
                        if current is not None:
                            return jsonify(
                                {"error": "recent_authentication_required"}
                            ), 409
                        return jsonify({"error": "invalid_session"}), 401
                    try:
                        states = hosted_channel_setup.test_delivery(  # type: ignore[union-attr]
                            session.context,
                            principal_id=session.principal_id,
                            session_id=session.id,
                            provider="slack",
                        )
                    except ValueError:
                        return jsonify({"error": "invalid_channel_setup"}), 400
                    except Exception:
                        return jsonify({"error": "channel_delivery_unavailable"}), 503
                    return jsonify(_public_channel_installations(states))

                if hosted_channel_lifecycle_enabled:

                    @app.delete("/v1/onboarding/channel-installations/slack")
                    def disconnect_slack_destination():
                        if not request.is_json:
                            return jsonify({"error": "invalid_request"}), 400
                        payload = request.get_json(silent=True)
                        if not isinstance(payload, dict) or payload != {
                            "confirmation": "disconnect"
                        }:
                            return jsonify({"error": "invalid_request"}), 400
                        session = _authorize_mutation(
                            request,
                            expected_origin,
                            sessions,  # type: ignore[arg-type]
                            recent=True,
                        )
                        if session is None:
                            current = _authorize_mutation(
                                request,
                                expected_origin,
                                sessions,  # type: ignore[arg-type]
                            )
                            if current is not None:
                                return jsonify(
                                    {"error": "recent_authentication_required"}
                                ), 409
                            return jsonify({"error": "invalid_session"}), 401
                        try:
                            states = hosted_channel_setup.disconnect(  # type: ignore[union-attr]
                                session.context,
                                principal_id=session.principal_id,
                                session_id=session.id,
                                provider="slack",
                            )
                            onboarding = hosted_onboarding.read(  # type: ignore[union-attr]
                                session.context,
                                principal_id=session.principal_id,
                            )
                        except ValueError:
                            return jsonify(
                                {"error": "invalid_channel_disconnect"}
                            ), 400
                        except Exception:
                            return jsonify(
                                {"error": "channel_disconnect_unavailable"}
                            ), 503
                        return jsonify(
                            {
                                **_public_channel_installations(states),
                                "onboarding": _public_onboarding(onboarding)
                                if onboarding is not None
                                else None,
                            }
                        )

            if hosted_channel_lifecycle_enabled:

                @app.delete("/v1/onboarding/channel-installations/google-chat")
                def disconnect_google_chat_destination():
                    if not request.is_json:
                        return jsonify({"error": "invalid_request"}), 400
                    payload = request.get_json(silent=True)
                    if not isinstance(payload, dict) or payload != {
                        "confirmation": "disconnect"
                    }:
                        return jsonify({"error": "invalid_request"}), 400
                    session = _authorize_mutation(
                        request,
                        expected_origin,
                        sessions,  # type: ignore[arg-type]
                        recent=True,
                    )
                    if session is None:
                        current = _authorize_mutation(
                            request,
                            expected_origin,
                            sessions,  # type: ignore[arg-type]
                        )
                        if current is not None:
                            return jsonify(
                                {"error": "recent_authentication_required"}
                            ), 409
                        return jsonify({"error": "invalid_session"}), 401
                    try:
                        states = hosted_channel_setup.disconnect(  # type: ignore[union-attr]
                            session.context,
                            principal_id=session.principal_id,
                            session_id=session.id,
                            provider="google_chat",
                        )
                        onboarding = hosted_onboarding.read(  # type: ignore[union-attr]
                            session.context, principal_id=session.principal_id
                        )
                    except ValueError:
                        return jsonify({"error": "invalid_channel_disconnect"}), 400
                    except Exception:
                        return jsonify({"error": "channel_disconnect_unavailable"}), 503
                    return jsonify(
                        {
                            **_public_channel_installations(states),
                            "onboarding": _public_onboarding(onboarding)
                            if onboarding is not None
                            else None,
                        }
                    )

    @app.errorhandler(400)
    def bad_request(_error):
        return jsonify({"error": "invalid_request"}), 400

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"error": "not_found"}), 404

    @app.errorhandler(405)
    def method_not_allowed(_error):
        return jsonify({"error": "method_not_allowed"}), 405

    return app


def _same_origin_request(request, expected_origin: str) -> bool:
    return (
        request.headers.get("Origin") == expected_origin
        and request.headers.get("Sec-Fetch-Site") == "same-origin"
    )


def _authorize_mutation(
    request,
    expected_origin: str,
    sessions: SessionRepository,
    *,
    recent: bool = False,
):
    if not _same_origin_request(request, expected_origin):
        return None
    token = request.cookies.get(SESSION_COOKIE, "")
    csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
    csrf_header = request.headers.get("X-Attune-CSRF", "")
    if not csrf_cookie or not hmac.compare_digest(csrf_cookie, csrf_header):
        return None
    try:
        if recent:
            return sessions.authorize_recent(token, csrf_cookie)
        return sessions.authorize(token, csrf_cookie)
    except Exception:
        return None


def _read_session(request, sessions: SessionRepository):
    token = request.cookies.get(SESSION_COOKIE, "")
    try:
        return sessions.read(token)
    except Exception:
        return None


def _public_onboarding(state):
    if state is None:
        return {"schema_version": 1, "status": "not_started", "steps": {}}
    return {
        "schema_version": state.schema_version,
        "status": state.status,
        "steps": {
            "workspace": state.workspace,
            "channels": state.channels,
            "policy": state.policy,
            "activation": state.activation,
        },
    }


def _public_policy(status: str) -> dict:
    return {
        "schema_version": 1,
        "profile": "private_alpha_read_only",
        "status": status,
        "maximum_risk": "R0",
        "automatic": ["Verify the read-only Gmail and Calendar connection"],
        "excluded": [
            "Send messages or email",
            "Change calendar events",
            "Delete or share provider data",
        ],
    }


def _public_channels(preferences, status: str) -> dict:
    return {
        "schema_version": 1,
        "status": status,
        "interaction_channels": (
            list(preferences.interaction_channels) if preferences else []
        ),
        "brief_channels": list(preferences.brief_channels) if preferences else [],
        "options": [
            {"id": "google_chat", "label": "Google Chat"},
            {"id": "slack", "label": "Slack"},
        ],
        "installation": "required",
    }


def _public_deletion_request(item) -> dict:
    if item is None:
        return {"schema_version": 1, "status": "none"}
    (
        request_id,
        status,
        requested_at,
        grace_expires_at,
        cancelled_at,
        completed_at,
        failure_code,
    ) = item
    return {
        "schema_version": 1,
        "id": str(request_id),
        "status": status,
        "requested_at": requested_at.isoformat(),
        "grace_expires_at": grace_expires_at.isoformat(),
        "cancelled_at": cancelled_at.isoformat() if cancelled_at else None,
        "completed_at": completed_at.isoformat() if completed_at else None,
        "failure_code": failure_code,
    }


def _public_export(item) -> dict:
    return {
        "id": str(item.id),
        "scope": item.scope,
        "state": item.state,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "ready_at": item.ready_at.isoformat() if item.ready_at else None,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "archive_bytes": item.archive_bytes,
        "download_available": item.state == "ready",
    }


def _public_model_profile(item) -> dict:
    return {
        "schema_version": 1,
        "profile": item.profile if item is not None else "standard",
        "revision": item.revision if item is not None else 0,
        "options": [
            {"id": "standard", "label": "Standard"},
            {"id": "premium", "label": "Premium"},
        ],
    }


def _public_usage(items) -> dict:
    return {
        "schema_version": 1,
        "window_days": 30,
        "items": [
            {
                "date": item.usage_date.isoformat(),
                "task": item.task,
                "profile": item.profile,
                "request_count": item.request_count,
                "input_tokens": item.input_tokens,
                "output_tokens": item.output_tokens,
                "failure_count": item.failure_count,
            }
            for item in items
        ],
    }


def _public_channel_installations(states) -> dict:
    return {
        "schema_version": 1,
        "providers": [
            {
                "provider": state.provider,
                "selected": state.selected,
                "setup_state": state.setup_state,
                "destination_state": state.destination_state,
            }
            for state in states
        ],
        "destination_policy": "owner_dm_only",
        "test_delivery": "required",
    }
