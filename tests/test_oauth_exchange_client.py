from attune.hosted.oauth_exchange_client import PrivateOAuthExchangeClient


class Response:
    status_code = 204


class Session:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response()


def test_callback_client_uses_workload_identity_and_fixed_private_endpoint():
    session = Session()
    audiences = []
    client = PrivateOAuthExchangeClient(
        "https://oauth-exchange.run.app",
        "https://oauth-exchange.internal",
        session=session,
        token_provider=lambda audience: audiences.append(audience) or "signed-token",
    )

    assert client.exchange(code="code", state="state", binding="binding") is True
    assert audiences == ["https://oauth-exchange.internal"]
    assert session.calls == [
        (
            "https://oauth-exchange.run.app/v1/oauth/google/exchange",
            {
                "json": {"code": "code", "state": "state", "binding": "binding"},
                "headers": {
                    "Authorization": "Bearer signed-token",
                    "Accept": "application/json",
                },
                "timeout": 10,
                "allow_redirects": False,
            },
        )
    ]
