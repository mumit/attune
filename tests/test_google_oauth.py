from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from attune.hosted.google_oauth import (
    GOOGLE_CERTS_URL,
    FixedGoogleCertRequest,
    GoogleAuthorizationCodeProvider,
    GoogleOAuthClientSecret,
)
from attune.hosted.google_provider import GOOGLE_TOKEN_URL, ProviderFailure

REDIRECT = "https://dev.attune.mumit.org/oauth/google/callback"


class SecretClient:
    def __init__(self, document):
        self.document = document
        self.calls = []

    def access_secret_version(self, *, request):
        self.calls.append(request)
        return SimpleNamespace(
            payload=SimpleNamespace(data=json.dumps(self.document).encode())
        )


class Response:
    def __init__(self, body, status=200):
        self.status_code = status
        self.raw = Raw(json.dumps(body).encode())
        self.closed = False

    def close(self):
        self.closed = True


class Raw:
    def __init__(self, value):
        self.value = value

    def read(self, size, decode_content=False):
        assert decode_content is True
        return self.value[:size]


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_certificate_request_rejects_every_noncanonical_request_before_network():
    request = FixedGoogleCertRequest(GOOGLE_CERTS_URL)

    with pytest.raises(ProviderFailure):
        request("https://attacker.example/certs")
    with pytest.raises(ProviderFailure):
        request(GOOGLE_CERTS_URL, method="POST")
    with pytest.raises(ProviderFailure):
        request(GOOGLE_CERTS_URL, body=b"unexpected")


def client_secret():
    return GoogleOAuthClientSecret(
        "projects/test/secrets/google-oauth-client",
        client=SecretClient(
            {
                "web": {
                    "client_id": "client.apps.googleusercontent.com",
                    "client_secret": "restricted",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": GOOGLE_TOKEN_URL,
                    "redirect_uris": [REDIRECT],
                }
            }
        ),
    )


def test_exchange_validates_nonce_scope_and_minimizes_stored_credential():
    nonce = "n" * 43
    scopes = ("openid", "email")
    response = Response(
        {
            "access_token": "access",
            "refresh_token": "refresh",
            "id_token": "signed-id-token",
            "token_type": "Bearer",
            "scope": "email openid",
        }
    )
    session = Session(response)
    provider = GoogleAuthorizationCodeProvider(
        client_secret(),
        session=session,
        id_token_verifier=lambda token, audience: {
            "iss": "https://accounts.google.com",
            "sub": "google-subject",
            "nonce": nonce,
        },
    )
    credential = provider.exchange(
        authorization_code="code",
        pkce_verifier="v" * 64,
        nonce_hash=hashlib.sha256(nonce.encode()).digest(),
        redirect_uri=REDIRECT,
        scopes=scopes,
    )
    assert session.calls[0][0] == GOOGLE_TOKEN_URL
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[0][1]["data"]["code_verifier"] == "v" * 64
    assert credential == {
        "refresh_token": "refresh",
        "client_id": "client.apps.googleusercontent.com",
        "client_secret": "restricted",
        "token_uri": GOOGLE_TOKEN_URL,
        "scopes": ["openid", "email"],
        "issuer": "https://accounts.google.com",
        "subject_hash": hashlib.sha256(b"google-subject").hexdigest(),
    }
    assert "access" not in repr(credential)
    assert response.closed


@pytest.mark.parametrize(
    "change",
    [
        {"refresh_token": None},
        {"scope": "openid"},
        {"token_type": "MAC"},
    ],
)
def test_exchange_rejects_incomplete_or_scope_changed_response(change):
    body = {
        "access_token": "access",
        "refresh_token": "refresh",
        "id_token": "signed-id-token",
        "token_type": "Bearer",
        "scope": "openid email",
    }
    body.update(change)
    provider = GoogleAuthorizationCodeProvider(
        client_secret(),
        session=Session(Response(body)),
        id_token_verifier=lambda token, audience: {
            "iss": "https://accounts.google.com",
            "sub": "subject",
            "nonce": "n" * 43,
        },
    )
    with pytest.raises(ProviderFailure):
        provider.exchange(
            authorization_code="code",
            pkce_verifier="v" * 64,
            nonce_hash=hashlib.sha256(("n" * 43).encode()).digest(),
            redirect_uri=REDIRECT,
            scopes=("openid", "email"),
        )


def test_exchange_accepts_only_the_fixed_google_email_scope_equivalence():
    body = {
        "access_token": "access",
        "refresh_token": "refresh",
        "id_token": "signed-id-token",
        "token_type": "Bearer",
        "scope": (
            "openid email https://www.googleapis.com/auth/userinfo.email "
            "https://www.googleapis.com/auth/gmail.readonly "
            "https://www.googleapis.com/auth/calendar.readonly"
        ),
    }
    provider = GoogleAuthorizationCodeProvider(
        client_secret(),
        session=Session(Response(body)),
        id_token_verifier=lambda token, audience: {
            "iss": "https://accounts.google.com",
            "sub": "subject",
            "nonce": "n" * 43,
        },
    )

    credential = provider.exchange(
        authorization_code="code",
        pkce_verifier="v" * 64,
        nonce_hash=hashlib.sha256(("n" * 43).encode()).digest(),
        redirect_uri=REDIRECT,
        scopes=(
            "openid",
            "email",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
        ),
    )

    assert credential["scopes"] == [
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
    ]

    extra_scope_body = dict(body)
    extra_scope_body["scope"] += " https://www.googleapis.com/auth/gmail.modify"
    provider_with_extra_scope = GoogleAuthorizationCodeProvider(
        client_secret(),
        session=Session(Response(extra_scope_body)),
        id_token_verifier=lambda token, audience: {
            "iss": "https://accounts.google.com",
            "sub": "subject",
            "nonce": "n" * 43,
        },
    )
    with pytest.raises(ProviderFailure):
        provider_with_extra_scope.exchange(
            authorization_code="code",
            pkce_verifier="v" * 64,
            nonce_hash=hashlib.sha256(("n" * 43).encode()).digest(),
            redirect_uri=REDIRECT,
            scopes=(
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/calendar.readonly",
            ),
        )


def test_exchange_rejects_wrong_nonce_or_redirect_before_storage():
    response = Response(
        {
            "access_token": "access",
            "refresh_token": "refresh",
            "id_token": "signed-id-token",
            "token_type": "Bearer",
            "scope": "openid email",
        }
    )
    provider = GoogleAuthorizationCodeProvider(
        client_secret(),
        session=Session(response),
        id_token_verifier=lambda token, audience: {
            "iss": "https://accounts.google.com",
            "sub": "subject",
            "nonce": "wrong",
        },
    )
    with pytest.raises(ProviderFailure):
        provider.exchange(
            authorization_code="code",
            pkce_verifier="v" * 64,
            nonce_hash=bytes(32),
            redirect_uri=REDIRECT,
            scopes=("openid", "email"),
        )
    with pytest.raises(ProviderFailure):
        provider.exchange(
            authorization_code="code",
            pkce_verifier="v" * 64,
            nonce_hash=bytes(32),
            redirect_uri="https://attacker.example/callback",
            scopes=("openid", "email"),
        )
