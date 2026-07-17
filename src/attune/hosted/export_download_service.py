"""Public, same-origin, one-time customer export download HTTP boundary."""

from __future__ import annotations

import re
from uuid import UUID, uuid4

from .export_download import CustomerExportDownloadService

HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def create_app(expected_host: str, downloads: CustomerExportDownloadService):
    from flask import Flask, Response, jsonify, request

    if not isinstance(expected_host, str) or not HOSTNAME.fullmatch(expected_host):
        raise ValueError("expected download host must be a DNS hostname")
    if downloads is None:
        raise ValueError("download service is required")
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=2048, TRUSTED_HOSTS=[expected_host])
    expected_origin = f"https://{expected_host}"

    @app.after_request
    def headers(response: Response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'none'"
        )
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/healthz")
    def health():
        return jsonify({"status": "ok"})

    @app.post("/v1/export-download")
    def download():
        if (
            request.headers.get("Origin") != expected_origin
            or request.headers.get("Sec-Fetch-Site") != "same-origin"
            or not request.is_json
        ):
            return jsonify({"error": "invalid_download"}), 401
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {"grant_id", "secret"}:
            return jsonify({"error": "invalid_download"}), 400
        try:
            grant_id = UUID(payload["grant_id"])
            secret = payload["secret"]
        except (TypeError, ValueError):
            return jsonify({"error": "invalid_download"}), 400
        if (
            not isinstance(secret, str)
            or not 40 <= len(secret) <= 64
            or not secret.isascii()
        ):
            return jsonify({"error": "invalid_download"}), 400
        try:
            archive = downloads.download(grant_id, secret, run_id=uuid4())
        except Exception:
            return jsonify({"error": "download_unavailable"}), 503
        if archive is None:
            return jsonify({"error": "download_not_available"}), 409
        response = Response(archive, status=200, content_type="application/zip")
        response.headers["Content-Disposition"] = (
            'attachment; filename="attune-account-export.zip"'
        )
        response.headers["Content-Length"] = str(len(archive))
        return response

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({"error": "not_found"}), 404

    @app.errorhandler(405)
    def method_not_allowed(_error):
        return jsonify({"error": "method_not_allowed"}), 405

    return app
