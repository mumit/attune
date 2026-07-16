"""Locked hosted control plane with a disabled-by-default identity boundary."""

from __future__ import annotations

import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol
from urllib.parse import urlencode

from .identity import IdentityRefused, VerifiedIdentity, verify_identity_platform_token
from .identity_session import (
    IdentitySession,
    IdentitySessionSecrets,
    create_identity_session_secrets,
)
from .oauth_transaction import create_oauth_transaction_secrets

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

    def revoke(self, token: str, csrf: str) -> bool: ...


class GoogleOAuthStartRepository(Protocol):
    def start(self, context, **kwargs): ...

    def is_connected(self, context, *, principal_id): ...


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
