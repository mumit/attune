"""Private HTTP adapter for authenticated Cloud Tasks delivery."""

from __future__ import annotations

import logging

from .service_metrics import instrument_service_metrics
from .worker_dispatch import WorkerDispatcher

LOG = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 4096


def create_app(dispatcher: WorkerDispatcher):
    from flask import Flask, jsonify, request

    app = Flask(__name__)
    instrument_service_metrics(app, service="worker")
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

    @app.get("/healthz")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/v1/tasks/dispatch")
    def dispatch():
        if not request.is_json:
            return jsonify({"error": "invalid_request"}), 400
        try:
            result = dispatcher.dispatch(
                authorization=request.headers.get("Authorization", ""),
                raw_body=request.get_data(cache=False),
            )
        except Exception as error:
            LOG.warning("worker dispatch failed (%s)", type(error).__name__)
            return jsonify({"error": "worker_unavailable"}), 503
        return ("", result.status_code)

    return app
