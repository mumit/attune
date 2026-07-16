"""Fixed Google authorization-code exchange and OIDC verification boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .google_provider import (
    GOOGLE_TOKEN_URL,
    MAX_ACCESS_TOKEN_CHARS,
    ProviderFailure,
    REQUEST_TIMEOUT,
    _json_response,
)

GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v1/certs"
MAX_CLIENT_SECRET_BYTES = 16_384
MAX_CERT_RESPONSE_BYTES = 65_536
GOOGLE_SCOPE_EQUIVALENTS = {
    "https://www.googleapis.com/auth/userinfo.email": "email",
}
LOG = logging.getLogger(__name__)


@dataclass(frozen=True, repr=False)
class GoogleOAuthClient:
    client_id: str
    client_secret: str
    redirect_uris: tuple[str, ...]

    def __repr__(self) -> str:
        return "GoogleOAuthClient(<redacted>)"


class GoogleOAuthClientSecret:
    """Load and validate the standard Google web-client JSON on demand."""

    def __init__(self, secret_resource: str, client: Any | None = None):
        if (
            not secret_resource.startswith("projects/")
            or "/secrets/" not in secret_resource
        ):
            raise ValueError("a full Secret Manager secret resource is required")
        if client is None:
            from google.cloud import secretmanager_v1

            client = secretmanager_v1.SecretManagerServiceClient()
        self._resource = secret_resource
        self._client = client

    def load(self) -> GoogleOAuthClient:
        response = self._client.access_secret_version(
            request={"name": f"{self._resource}/versions/latest"}
        )
        raw = bytes(response.payload.data)
        if not 1 <= len(raw) <= MAX_CLIENT_SECRET_BYTES:
            raise ProviderFailure("OAuth client secret is unavailable")
        try:
            document = json.loads(raw)
        except Exception as error:
            raise ProviderFailure("OAuth client secret is unavailable") from error
        return _client_document(document)


class GoogleAuthorizationCodeProvider:
    def __init__(
        self,
        client_secret: GoogleOAuthClientSecret,
        *,
        session: Any | None = None,
        id_token_verifier: Callable[[str, str], Mapping[str, Any]] | None = None,
    ):
        if session is None:
            import requests

            session = requests.Session()
            session.trust_env = False
        self._clients = client_secret
        self._session = session
        self._verify_id_token = id_token_verifier or _verify_google_id_token

    def exchange(
        self,
        *,
        authorization_code: str,
        pkce_verifier: str,
        nonce_hash: bytes,
        redirect_uri: str,
        scopes: Sequence[str],
    ) -> dict[str, Any]:
        client = self._clients.load()
        requested_scopes = tuple(scopes)
        if redirect_uri not in client.redirect_uris:
            raise ProviderFailure("OAuth redirect is unavailable")
        if (
            not 1 <= len(authorization_code) <= 4096
            or not all(0x21 <= ord(character) <= 0x7E for character in authorization_code)
            or not 43 <= len(pkce_verifier) <= 128
            or not pkce_verifier.replace("-", "A").replace("_", "A").isalnum()
            or not isinstance(nonce_hash, bytes)
            or len(nonce_hash) != 32
            or not 1 <= len(requested_scopes) <= 32
            or len(set(requested_scopes)) != len(requested_scopes)
            or "openid" not in requested_scopes
        ):
            raise ProviderFailure("OAuth exchange input is invalid")
        try:
            response = self._session.post(
                GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "client_id": client.client_id,
                    "client_secret": client.client_secret,
                    "redirect_uri": redirect_uri,
                    "code_verifier": pkce_verifier,
                },
                headers={"Accept": "application/json"},
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
                stream=True,
            )
        except Exception as error:
            LOG.warning("google_oauth_exchange_refused stage=token_request")
            raise ProviderFailure("token request failed") from error
        try:
            body = _json_response(response, expected_status=200)
        except ProviderFailure:
            LOG.warning("google_oauth_exchange_refused stage=token_endpoint")
            raise
        access_token = body.get("access_token")
        refresh_token = body.get("refresh_token")
        id_token = body.get("id_token")
        token_type = body.get("token_type")
        granted_scopes = body.get("scope")
        if (
            not isinstance(access_token, str)
            or not 1 <= len(access_token) <= MAX_ACCESS_TOKEN_CHARS
            or not isinstance(refresh_token, str)
            or not 1 <= len(refresh_token) <= 8192
            or not isinstance(id_token, str)
            or not 1 <= len(id_token) <= 16384
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
            or not isinstance(granted_scopes, str)
            or not _equivalent_scope_grant(granted_scopes, requested_scopes)
        ):
            LOG.warning("google_oauth_exchange_refused stage=token_response")
            raise ProviderFailure("invalid token response")
        try:
            claims = self._verify_id_token(id_token, client.client_id)
        except Exception as error:
            LOG.warning("google_oauth_exchange_refused stage=id_token")
            raise ProviderFailure("invalid ID token") from error
        nonce = claims.get("nonce")
        subject = claims.get("sub")
        issuer = claims.get("iss")
        if (
            not isinstance(nonce, str)
            or not hmac.compare_digest(
                hashlib.sha256(nonce.encode()).digest(), nonce_hash
            )
            or not isinstance(subject, str)
            or not 1 <= len(subject) <= 255
            or issuer not in {"accounts.google.com", "https://accounts.google.com"}
        ):
            LOG.warning("google_oauth_exchange_refused stage=id_token_binding")
            raise ProviderFailure("invalid ID token binding")
        return {
            "refresh_token": refresh_token,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "token_uri": GOOGLE_TOKEN_URL,
            "scopes": list(requested_scopes),
            "issuer": issuer,
            "subject_hash": hashlib.sha256(subject.encode()).hexdigest(),
        }


def _equivalent_scope_grant(granted: str, requested: Sequence[str]) -> bool:
    """Compare Google's fixed aliases without admitting an extra capability."""
    values = granted.split()
    if not values or any(not 1 <= len(value) <= 255 for value in values):
        return False
    normalized = {GOOGLE_SCOPE_EQUIVALENTS.get(value, value) for value in values}
    expected = {GOOGLE_SCOPE_EQUIVALENTS.get(value, value) for value in requested}
    return normalized == expected


def _client_document(value: Any) -> GoogleOAuthClient:
    if not isinstance(value, dict) or set(value) != {"web"}:
        raise ProviderFailure("OAuth client secret is unavailable")
    web = value["web"]
    required = {"client_id", "client_secret", "auth_uri", "token_uri", "redirect_uris"}
    if not isinstance(web, dict) or not required <= set(web):
        raise ProviderFailure("OAuth client secret is unavailable")
    if (
        web["token_uri"] != GOOGLE_TOKEN_URL
        or web["auth_uri"] != "https://accounts.google.com/o/oauth2/auth"
    ):
        raise ProviderFailure("OAuth client endpoint is unavailable")
    client_id, client_secret, redirects = (
        web["client_id"],
        web["client_secret"],
        web["redirect_uris"],
    )
    if (
        not isinstance(client_id, str)
        or not 1 <= len(client_id) <= 512
        or not isinstance(client_secret, str)
        or not 1 <= len(client_secret) <= 512
        or not isinstance(redirects, list)
        or not 1 <= len(redirects) <= 8
        or not all(
            isinstance(uri, str) and uri.startswith("https://") for uri in redirects
        )
        or len(set(redirects)) != len(redirects)
    ):
        raise ProviderFailure("OAuth client secret is unavailable")
    return GoogleOAuthClient(client_id, client_secret, tuple(redirects))


def _verify_google_id_token(token: str, audience: str) -> Mapping[str, Any]:
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(
        token,
        FixedGoogleCertRequest(GOOGLE_CERTS_URL),
        audience=audience,
        clock_skew_in_seconds=30,
    )


class _BoundedCertResponse:
    def __init__(self, status: int, headers: Mapping[str, str], data: bytes):
        self.status = status
        self.headers = headers
        self.data = data


class FixedGoogleCertRequest:
    """google-auth request adapter restricted to one reviewed certificate URL."""

    def __init__(self, expected_url: str):
        import requests

        if not isinstance(expected_url, str) or not expected_url.startswith(
            "https://www.googleapis.com/"
        ):
            raise ValueError("a fixed Google certificate URL is required")
        self._expected_url = expected_url
        self._session = requests.Session()
        self._session.trust_env = False

    def __call__(
        self, url, method="GET", body=None, headers=None, timeout=None, **kwargs
    ):
        if url != self._expected_url or method != "GET" or body is not None or kwargs:
            raise ProviderFailure("certificate request is invalid")
        response = self._session.get(
            self._expected_url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
            stream=True,
        )
        try:
            raw = response.raw.read(MAX_CERT_RESPONSE_BYTES + 1, decode_content=True)
            if len(raw) > MAX_CERT_RESPONSE_BYTES:
                raise ProviderFailure("certificate response is too large")
            return _BoundedCertResponse(response.status_code, response.headers, raw)
        finally:
            response.close()
