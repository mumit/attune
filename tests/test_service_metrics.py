"""Offline tests for the content-free hosted request-metrics hook.

Pins the exact field vocabulary from ``service_metrics.py``: that a request
line carries EXACTLY the seven fixed fields and nothing else, that a
templated (parameterized) route reports its rule -- never the raw path
carrying a real-looking ID -- that unmatched routes report ``"unmatched"``,
that no query string or header ever leaks, that a hook failure never
breaks the request it observes, and that all six hosted Flask app
factories are wired with their fixed service name.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("flask")

from flask import Flask

from attune.hosted import service_metrics
from attune.hosted.service_metrics import instrument_service_metrics

HTTP_REQUEST_FIELDS = {
    "metric",
    "service",
    "route",
    "method",
    "status_class",
    "status",
    "duration_ms",
}


def _build_app() -> Flask:
    app = Flask(__name__)
    instrument_service_metrics(app, service="test_service")

    @app.get("/v1/plain")
    def plain():
        return {"ok": True}

    @app.get("/v1/items/<uuid:item_id>")
    def item(item_id):
        return {"ok": True}

    @app.post("/v1/echo")
    def echo():
        return {"ok": True}

    @app.get("/v1/boom")
    def boom():
        raise RuntimeError("internal failure")

    return app


def _last_json_line(capsys) -> dict:
    out = capsys.readouterr().out.strip().splitlines()
    assert out, "expected at least one emitted metrics line"
    return json.loads(out[-1])


def test_emits_exactly_the_fixed_field_set_and_nothing_else(capsys):
    client = _build_app().test_client()
    response = client.get("/v1/plain")
    assert response.status_code == 200

    payload = _last_json_line(capsys)
    assert set(payload.keys()) == HTTP_REQUEST_FIELDS
    assert payload == {
        "metric": "http_request",
        "service": "test_service",
        "route": "/v1/plain",
        "method": "GET",
        "status_class": "2xx",
        "status": 200,
        "duration_ms": payload["duration_ms"],
    }
    assert isinstance(payload["duration_ms"], int)
    assert payload["duration_ms"] >= 0


def test_parameterized_route_reports_the_template_not_the_juicy_id(capsys):
    client = _build_app().test_client()
    juicy_id = "10000000-0000-4000-8000-000000000099"
    response = client.get(f"/v1/items/{juicy_id}?tenant=acme-corp&secret=shh")

    payload = _last_json_line(capsys)
    assert response.status_code == 200
    assert payload["route"] == "/v1/items/<uuid:item_id>"
    # The raw path, the query string, and any of its values must never
    # appear anywhere in the emitted line.
    rendered = json.dumps(payload)
    assert juicy_id not in rendered
    assert "acme-corp" not in rendered
    assert "shh" not in rendered
    assert "tenant" not in rendered
    assert "secret" not in rendered
    assert "?" not in rendered


def test_headers_and_body_never_leak(capsys):
    client = _build_app().test_client()
    response = client.post(
        "/v1/echo",
        data=b'{"tenant_id": "should-never-appear", "secret": "shh"}',
        content_type="application/json",
        headers={
            "Authorization": "Bearer super-secret-token",
            "User-Agent": "nosy-agent/1.0",
            "X-Forwarded-For": "203.0.113.7",
        },
    )
    assert response.status_code == 200

    payload = _last_json_line(capsys)
    assert set(payload.keys()) == HTTP_REQUEST_FIELDS
    rendered = json.dumps(payload)
    for forbidden in (
        "should-never-appear",
        "shh",
        "super-secret-token",
        "nosy-agent",
        "203.0.113.7",
        "Authorization",
    ):
        assert forbidden not in rendered


def test_unmatched_route_reports_unmatched(capsys):
    client = _build_app().test_client()
    response = client.get("/v1/does/not/exist")
    assert response.status_code == 404

    payload = _last_json_line(capsys)
    assert payload["route"] == "unmatched"
    assert payload["status"] == 404
    assert payload["status_class"] == "4xx"


def test_server_error_reports_5xx(capsys):
    app = _build_app()
    app.testing = False
    client = app.test_client()
    response = client.get("/v1/boom")
    assert response.status_code == 500

    payload = _last_json_line(capsys)
    assert payload["status"] == 500
    assert payload["status_class"] == "5xx"
    assert payload["route"] == "/v1/boom"


def test_before_hook_failure_never_breaks_the_request(monkeypatch, capsys):
    def _boom():
        raise RuntimeError("clock unavailable")

    monkeypatch.setattr(service_metrics.time, "monotonic", _boom)
    client = _build_app().test_client()
    response = client.get("/v1/plain")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    # No metric line survives a fully broken clock (both hooks fail closed);
    # the important pin is that the request itself was unaffected.


def test_after_hook_emit_failure_never_breaks_the_request(monkeypatch):
    def _boom(payload):
        raise RuntimeError("emit failed")

    monkeypatch.setattr(service_metrics, "_emit_metric_line", _boom)
    client = _build_app().test_client()
    response = client.get("/v1/plain")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def _app_factories():
    from attune.hosted import (
        channel_broker_service,
        control_plane_service,
        dispatch_broker_service,
        model_gateway_service,
        secret_broker_service,
        worker_service,
    )

    def control_plane_app():
        return control_plane_service.create_app("dev.example.test")

    def worker_app():
        return worker_service.create_app(object())

    def dispatch_broker_app():
        return dispatch_broker_service.create_app(
            object(),
            expected_audience="https://dispatch.example.test",
            expected_callers={
                "control_plane": "cp@example.iam.gserviceaccount.com",
                "ingress": "ingress@example.iam.gserviceaccount.com",
                "worker": "worker@example.iam.gserviceaccount.com",
            },
        )

    def secret_broker_app():
        return secret_broker_service.create_app(
            object(),
            expected_audience="https://secret.example.test",
            expected_control_plane="cp@example.iam.gserviceaccount.com",
            expected_worker="worker@example.iam.gserviceaccount.com",
            expected_oauth_exchange="oauthx@example.iam.gserviceaccount.com",
        )

    def model_gateway_app():
        return model_gateway_service.create_app(
            object(),
            expected_audience="https://model.example.test",
            expected_worker="worker@example.iam.gserviceaccount.com",
        )

    def channel_broker_app():
        return channel_broker_service.create_app(
            object(),
            expected_audience="https://channel.example.test",
            expected_ingress="ingress@example.iam.gserviceaccount.com",
            expected_control_plane="cp@example.iam.gserviceaccount.com",
            expected_worker="worker@example.iam.gserviceaccount.com",
        )

    return [
        ("control_plane", control_plane_app),
        ("worker", worker_app),
        ("dispatch_broker", dispatch_broker_app),
        ("secret_broker", secret_broker_app),
        ("model_gateway", model_gateway_app),
        ("channel_broker", channel_broker_app),
    ]


@pytest.mark.parametrize("service_name,factory", _app_factories())
def test_all_six_hosted_apps_are_wired_with_their_fixed_service_name(
    service_name, factory, capsys
):
    app = factory()
    client = app.test_client()
    if service_name == "control_plane":
        # This app pins TRUSTED_HOSTS to its exact configured hostname.
        response = client.get("/healthz", base_url="https://dev.example.test")
    else:
        response = client.get("/healthz")
    assert response.status_code == 200

    payload = _last_json_line(capsys)
    assert set(payload.keys()) == HTTP_REQUEST_FIELDS
    assert payload["metric"] == "http_request"
    assert payload["service"] == service_name
    assert payload["route"] == "/healthz"
    assert payload["status"] == 200
    assert payload["status_class"] == "2xx"


def test_emit_task_execution_ships_exactly_the_fixed_field_set(capsys):
    service_metrics.emit_task_execution(
        task="gmail.reconcile", outcome="succeeded", duration_ms=42
    )
    payload = _last_json_line(capsys)
    assert payload == {
        "metric": "task_execution",
        "task": "gmail.reconcile",
        "outcome": "succeeded",
        "duration_ms": 42,
    }


def test_emit_task_execution_failure_is_swallowed(monkeypatch):
    monkeypatch.setattr(
        service_metrics.json,
        "dumps",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    # Must not raise.
    service_metrics.emit_task_execution(task="x", outcome="failed", duration_ms=1)
