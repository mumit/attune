import time

from attune.hosted.model_gateway import EmbedResult, ModelResult
from attune.hosted.model_gateway_service import create_app

AUDIENCE = "https://attune-model.attune.internal"
WORKER = "attune-worker@example.iam.gserviceaccount.com"


class Gateway:
    def __init__(self, error=None, vector=(0.1, 0.2), usage=None):
        self.error = error
        self.vector = vector
        self.usage = usage
        self.calls = []
        self.embed_calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return ModelResult("bounded answer", self.usage)

    def embed(self, **kwargs):
        self.embed_calls.append(kwargs)
        if self.error:
            raise self.error
        return EmbedResult(tuple(self.vector), self.usage)


def claims(token, audience):
    now = int(time.time())
    return {
        "iss": "https://accounts.google.com",
        "aud": audience,
        "email": WORKER if token == "worker" else "attacker@example.com",
        "email_verified": True,
        "sub": token,
        "iat": now - 10,
        "exp": now + 300,
    }


def client(gateway, *, profiles_enabled=False):
    return create_app(
        gateway,
        expected_audience=AUDIENCE,
        expected_worker=WORKER,
        token_verifier=claims,
        profiles_enabled=profiles_enabled,
    ).test_client()


def request_body():
    return {
        "version": 1,
        "task": "converse",
        "messages": [{"role": "system", "content": "boundary"}],
    }


def test_service_requires_exact_worker_and_forwards_fixed_schema():
    gateway = Gateway()
    app = client(gateway)
    assert app.post(
        "/v1/models/complete",
        headers={"Authorization": "Bearer attacker"},
        json=request_body(),
    ).status_code == 403
    response = app.post(
        "/v1/models/complete",
        headers={"Authorization": "Bearer worker"},
        json=request_body(),
    )
    assert response.status_code == 200
    assert response.get_json() == {"text": "bounded answer", "usage": None}
    assert gateway.calls == [{
        "task": "converse",
        "messages": [{"role": "system", "content": "boundary"}],
        "profile": None,
    }]


def test_service_rejects_extra_authority_and_has_generic_failures():
    body = request_body()
    body["model"] = "caller-model"
    assert client(Gateway()).post(
        "/v1/models/complete",
        headers={"Authorization": "Bearer worker"},
        json=body,
    ).status_code == 400

    response = client(Gateway(ValueError("sensitive prompt"))).post(
        "/v1/models/complete",
        headers={"Authorization": "Bearer worker"},
        json=request_body(),
    )
    assert response.status_code == 400
    assert b"sensitive prompt" not in response.data

    response = client(Gateway(RuntimeError("secret credential"))).post(
        "/v1/models/complete",
        headers={"Authorization": "Bearer worker"},
        json=request_body(),
    )
    assert response.status_code == 503
    assert response.get_json() == {"error": "model_unavailable"}
    assert b"secret credential" not in response.data


def embed_body():
    return {"version": 1, "task": "embed", "input": "hello"}


def test_embed_endpoint_requires_exact_worker_and_forwards_fixed_schema():
    gateway = Gateway(vector=(0.5, -0.5))
    app = client(gateway)
    assert app.post(
        "/v1/models/embed",
        headers={"Authorization": "Bearer attacker"},
        json=embed_body(),
    ).status_code == 403
    response = app.post(
        "/v1/models/embed",
        headers={"Authorization": "Bearer worker"},
        json=embed_body(),
    )
    assert response.status_code == 200
    assert response.get_json() == {"vector": [0.5, -0.5], "usage": None}
    assert gateway.embed_calls == [{"text": "hello", "profile": None}]


def test_embed_endpoint_rejects_extra_authority_and_has_generic_failures():
    body = embed_body()
    body["model"] = "caller-model"
    assert client(Gateway()).post(
        "/v1/models/embed",
        headers={"Authorization": "Bearer worker"},
        json=body,
    ).status_code == 400

    response = client(Gateway(ValueError("sensitive input"))).post(
        "/v1/models/embed",
        headers={"Authorization": "Bearer worker"},
        json=embed_body(),
    )
    assert response.status_code == 400
    assert b"sensitive input" not in response.data

    response = client(Gateway(RuntimeError("secret credential"))).post(
        "/v1/models/embed",
        headers={"Authorization": "Bearer worker"},
        json=embed_body(),
    )
    assert response.status_code == 503
    assert response.get_json() == {"error": "model_unavailable"}
    assert b"secret credential" not in response.data


# -- Per-tenant model profiles (docs/future-state.md Phase 6 "hosted
# operations"), gated by ATTUNE_ENABLE_TENANT_MODEL_PROFILES -----------------


def test_profile_field_is_refused_when_the_gate_is_off():
    """Gate off: a profile field arriving on the wire is refused outright,
    never silently ignored -- independent defense-in-depth from whatever the
    worker's own gate did or didn't do."""
    gateway = Gateway()
    body = request_body()
    body["profile"] = "premium"
    response = client(gateway, profiles_enabled=False).post(
        "/v1/models/complete",
        headers={"Authorization": "Bearer worker"},
        json=body,
    )
    assert response.status_code == 400
    assert gateway.calls == []


def test_profile_field_is_forwarded_when_the_gate_is_on():
    gateway = Gateway()
    body = request_body()
    body["profile"] = "premium"
    response = client(gateway, profiles_enabled=True).post(
        "/v1/models/complete",
        headers={"Authorization": "Bearer worker"},
        json=body,
    )
    assert response.status_code == 200
    assert gateway.calls == [{
        "task": "converse",
        "messages": [{"role": "system", "content": "boundary"}],
        "profile": "premium",
    }]


def test_omitted_profile_is_byte_identical_regardless_of_the_gate():
    """Pin: with the gate ON but no profile field sent at all, the call to
    the gateway is identical to the gate-off path -- ``profile=None``."""
    gateway = Gateway()
    response = client(gateway, profiles_enabled=True).post(
        "/v1/models/complete",
        headers={"Authorization": "Bearer worker"},
        json=request_body(),
    )
    assert response.status_code == 200
    assert gateway.calls == [{
        "task": "converse",
        "messages": [{"role": "system", "content": "boundary"}],
        "profile": None,
    }]


def test_an_invalid_profile_shape_is_refused_even_with_the_gate_on():
    gateway = Gateway()
    body = request_body()
    body["profile"] = "Not Valid!"
    response = client(gateway, profiles_enabled=True).post(
        "/v1/models/complete",
        headers={"Authorization": "Bearer worker"},
        json=body,
    )
    assert response.status_code == 400
    assert gateway.calls == []


def test_embed_profile_field_gating_mirrors_complete():
    gateway = Gateway()
    body = embed_body()
    body["profile"] = "premium"
    refused = client(gateway, profiles_enabled=False).post(
        "/v1/models/embed",
        headers={"Authorization": "Bearer worker"},
        json=body,
    )
    assert refused.status_code == 400
    allowed = client(gateway, profiles_enabled=True).post(
        "/v1/models/embed",
        headers={"Authorization": "Bearer worker"},
        json=body,
    )
    assert allowed.status_code == 200
    assert gateway.embed_calls == [{"text": "hello", "profile": "premium"}]


def test_usage_is_reported_in_the_response_envelope_when_the_gateway_has_it():
    from attune.hosted.model_gateway import TokenUsage

    gateway = Gateway(usage=TokenUsage(11, 22))
    response = client(gateway).post(
        "/v1/models/complete",
        headers={"Authorization": "Bearer worker"},
        json=request_body(),
    )
    assert response.get_json()["usage"] == {"input_tokens": 11, "output_tokens": 22}
