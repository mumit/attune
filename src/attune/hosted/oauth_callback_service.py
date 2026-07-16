"""Credential-free public scrubber for dormant hosted OAuth callbacks."""

from __future__ import annotations

import re
from typing import Protocol

HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
MAX_CALLBACK_QUERY_BYTES = 4096
OAUTH_BINDING_COOKIE = "__Secure-attune_oauth_binding"
GOOGLE_AUTHORIZATION_ISSUER = "https://accounts.google.com"


class OAuthExchangeClient(Protocol):
    def exchange(self, *, code: str, state: str, binding: str) -> bool: ...


def create_app(
    expected_host: str,
    *,
    oauth_enabled: bool = False,
    exchange: OAuthExchangeClient | None = None,
):
    """Create the credential-free public OAuth callback scrubber."""
    from flask import Flask, Response, abort, jsonify, redirect, request

    if not isinstance(expected_host, str) or not HOSTNAME.fullmatch(expected_host):
        raise ValueError("expected OAuth callback host must be a DNS hostname")
    if oauth_enabled and exchange is None:
        raise ValueError("enabled OAuth callback requires a private exchange client")
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=1024,
        TRUSTED_HOSTS=[expected_host],
    )

    @app.after_request
    def security_headers(response: Response):
        response.headers["Cache-Control"] = "no-store"
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
        mode = "oauth_enabled" if oauth_enabled else "oauth_not_activated"
        return jsonify({"status": "ok", "mode": mode})

    @app.get("/oauth/google/callback")
    def google_callback():
        # Do not parse, copy, persist, exchange, or log query parameters while
        # OAuth is dormant. The redirect immediately removes them from the URL.
        if len(request.query_string) > MAX_CALLBACK_QUERY_BYTES:
            abort(400)
        if not oauth_enabled:
            return redirect("/", code=303)
        response = None
        if (
            len(request.args.getlist("iss")) != 1
            or request.args["iss"] != GOOGLE_AUTHORIZATION_ISSUER
        ):
            abort(400)
        if "error" in request.args:
            try:
                valid_denial = (
                    len(request.args.getlist("error")) == 1
                    and len(request.args.getlist("state")) == 1
                    and request.args["error"] == "access_denied"
                    and bool(request.cookies.get(OAUTH_BINDING_COOKIE, ""))
                    and _opaque_value(request.args["state"])
                )
            except ValueError:
                valid_denial = False
            if valid_denial:
                response = redirect("/?workspace=denied", code=303)
            else:
                abort(400)
        else:
            if (
                len(request.args.getlist("code")) != 1
                or len(request.args.getlist("state")) != 1
            ):
                abort(400)
            code = request.args["code"]
            state = request.args["state"]
            binding = request.cookies.get(OAUTH_BINDING_COOKIE, "")
            try:
                installed = bool(
                    exchange.exchange(  # type: ignore[union-attr]
                        code=code, state=state, binding=binding
                    )
                )
            except Exception:
                installed = False
            response = redirect(
                "/?workspace=connected" if installed else "/?workspace=failed",
                code=303,
            )
        response.delete_cookie(
            OAUTH_BINDING_COOKIE,
            path="/oauth/google/callback",
            secure=True,
            httponly=True,
            samesite="Lax",
        )
        return response

    @app.errorhandler(400)
    def bad_request(_error):
        response = jsonify({"error": "invalid_callback"})
        response.status_code = 400
        response.delete_cookie(
            OAUTH_BINDING_COOKIE,
            path="/oauth/google/callback",
            secure=True,
            httponly=True,
            samesite="Lax",
        )
        return response

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"error": "not_found"}), 404

    @app.errorhandler(405)
    def method_not_allowed(_error):
        return jsonify({"error": "method_not_allowed"}), 405

    return app


def _opaque_value(value: str) -> bool:
    return (
        isinstance(value, str)
        and 43 <= len(value) <= 128
        and all(character.isalnum() or character in "-_" for character in value)
    )
