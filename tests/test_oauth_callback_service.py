from __future__ import annotations

import pytest

from attune.hosted.oauth_callback_service import MAX_CALLBACK_QUERY_BYTES, create_app


class Exchange:
    def __init__(self, installed=True):
        self.installed = installed
        self.calls = []

    def exchange(self, **kwargs):
        self.calls.append(kwargs)
        return self.installed


def test_callback_is_inert_and_strips_the_credential_bearing_url():
    client = create_app("dev.attune.example").test_client()

    response = client.get(
        "/oauth/google/callback?code=secret-code&state=secret-state",
        base_url="https://dev.attune.example",
    )

    assert response.status_code == 303
    assert response.headers["Location"] == "/"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert b"secret-code" not in response.data
    assert b"secret-state" not in response.data


def test_callback_refuses_oversized_query_without_reflecting_it():
    client = create_app("dev.attune.example").test_client()
    secret = "x" * (MAX_CALLBACK_QUERY_BYTES + 1)

    response = client.get(
        f"/oauth/google/callback?code={secret}",
        base_url="https://dev.attune.example",
    )

    assert response.status_code == 400
    assert secret.encode() not in response.data


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_callback_accepts_only_get(method: str):
    client = create_app("dev.attune.example").test_client()

    response = getattr(client, method)(
        "/oauth/google/callback", base_url="https://dev.attune.example"
    )

    assert response.status_code == 405


def test_callback_rejects_host_confusion():
    response = (
        create_app("dev.attune.example")
        .test_client()
        .get("/oauth/google/callback", base_url="https://attacker.example")
    )
    assert response.status_code == 400


def test_callback_health_is_content_free():
    response = (
        create_app("dev.attune.example")
        .test_client()
        .get("/healthz", base_url="https://dev.attune.example")
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "status": "ok",
        "mode": "oauth_not_activated",
    }


def test_enabled_callback_scrubs_and_forwards_only_bounded_material():
    exchange = Exchange()
    client = create_app(
        "dev.attune.example", oauth_enabled=True, exchange=exchange
    ).test_client()
    client.set_cookie(
        "__Secure-attune_oauth_binding",
        "b" * 43,
        domain="dev.attune.example",
        path="/oauth/google/callback",
        secure=True,
    )
    response = client.get(
        "/oauth/google/callback?iss=https%3A%2F%2Faccounts.google.com"
        "&code=provider-code&state=" + "s" * 43,
        base_url="https://dev.attune.example",
    )

    assert response.status_code == 303
    assert response.headers["Location"] == "/?workspace=connected"
    assert exchange.calls == [
        {"code": "provider-code", "state": "s" * 43, "binding": "b" * 43}
    ]
    assert "provider-code" not in response.get_data(as_text=True)
    assert any(
        "__Secure-attune_oauth_binding=;" in value
        and "Path=/oauth/google/callback" in value
        for value in response.headers.getlist("Set-Cookie")
    )


def test_enabled_callback_fails_closed_on_duplicate_or_invalid_authority_parameters():
    exchange = Exchange()
    client = create_app(
        "dev.attune.example", oauth_enabled=True, exchange=exchange
    ).test_client()
    for query in (
        "iss=https%3A%2F%2Faccounts.google.com&code=one&code=two&state=three",
        "iss=https%3A%2F%2Faccounts.google.com&code=one&state=two&state=three",
        "iss=one&iss=two&code=one&state=three",
        "iss=https%3A%2F%2Fattacker.example&code=one&state=three",
        "code=one&state=three",
        "iss=https%3A%2F%2Faccounts.google.com&state=three",
    ):
        response = client.get(
            f"/oauth/google/callback?{query}",
            base_url="https://dev.attune.example",
        )
        assert response.status_code == 400
        assert any(
            "__Secure-attune_oauth_binding=;" in value
            for value in response.headers.getlist("Set-Cookie")
        )
    assert exchange.calls == []


def test_enabled_callback_ignores_and_scrubs_non_authoritative_extensions():
    exchange = Exchange()
    client = create_app(
        "dev.attune.example", oauth_enabled=True, exchange=exchange
    ).test_client()
    client.set_cookie(
        "__Secure-attune_oauth_binding",
        "b" * 43,
        domain="dev.attune.example",
        path="/oauth/google/callback",
        secure=True,
    )

    response = client.get(
        "/oauth/google/callback?iss=https%3A%2F%2Faccounts.google.com"
        "&code=provider-code&state="
        + "s" * 43
        + "&provider_extension=ignored&provider_extension=also-ignored",
        base_url="https://dev.attune.example",
    )

    assert response.status_code == 303
    assert response.headers["Location"] == "/?workspace=connected"
    assert exchange.calls == [
        {"code": "provider-code", "state": "s" * 43, "binding": "b" * 43}
    ]
    assert "provider_extension" not in response.get_data(as_text=True)


def test_enabled_callback_accepts_exact_issuer_on_user_denial():
    exchange = Exchange()
    client = create_app(
        "dev.attune.example", oauth_enabled=True, exchange=exchange
    ).test_client()
    client.set_cookie(
        "__Secure-attune_oauth_binding",
        "b" * 43,
        domain="dev.attune.example",
        path="/oauth/google/callback",
        secure=True,
    )

    response = client.get(
        "/oauth/google/callback?iss=https%3A%2F%2Faccounts.google.com"
        "&error=access_denied&state=" + "s" * 43 + "&provider_extension=ignored",
        base_url="https://dev.attune.example",
    )

    assert response.status_code == 303
    assert response.headers["Location"] == "/?workspace=denied"
    assert exchange.calls == []


def test_enabled_callback_requires_private_exchange_client():
    with pytest.raises(ValueError):
        create_app("dev.attune.example", oauth_enabled=True)


@pytest.mark.parametrize(
    "host", ["", "LOCALHOST", "https://dev.attune.example", "dev_attune.example"]
)
def test_callback_requires_an_exact_dns_hostname(host: str):
    with pytest.raises(ValueError):
        create_app(host)
