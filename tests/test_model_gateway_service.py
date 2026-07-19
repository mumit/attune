import time

from attune.hosted.model_gateway import EmbedResult, ModelResult
from attune.hosted.model_gateway_service import create_app

AUDIENCE = "https://attune-model.attune.internal"
WORKER = "attune-worker@example.iam.gserviceaccount.com"


class Gateway:
    def __init__(self, error=None, vector=(0.1, 0.2)):
        self.error = error
        self.vector = vector
        self.calls = []
        self.embed_calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return ModelResult("bounded answer")

    def embed(self, **kwargs):
        self.embed_calls.append(kwargs)
        if self.error:
            raise self.error
        return EmbedResult(tuple(self.vector))


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


def client(gateway):
    return create_app(
        gateway,
        expected_audience=AUDIENCE,
        expected_worker=WORKER,
        token_verifier=claims,
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
    assert response.get_json() == {"text": "bounded answer"}
    assert gateway.calls == [{
        "task": "converse",
        "messages": [{"role": "system", "content": "boundary"}],
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
    assert response.get_json() == {"vector": [0.5, -0.5]}
    assert gateway.embed_calls == [{"text": "hello"}]


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
