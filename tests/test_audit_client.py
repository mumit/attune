from __future__ import annotations

from uuid import UUID

import pytest

from attune.hosted.audit_client import AuditWriterClient

INTENT = UUID("10000000-0000-4000-8000-000000000501")
EVENT = UUID("10000000-0000-4000-8000-000000000502")
URL = "https://attune-audit.example.run.app"


class Response:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {
            "status": "written",
            "audit_event_id": str(EVENT),
        }

    def json(self):
        return self._body


class Session:
    def __init__(self, response=None):
        self.response = response or Response()
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_audit_writer_uses_exact_audience_and_disables_redirects():
    session = Session()
    audiences = []
    writer = AuditWriterClient(
        URL,
        token_provider=lambda audience: audiences.append(audience) or "token",
        session=session,
    )
    assert writer.write(INTENT)
    assert audiences == [URL]
    assert session.calls == [
        (
            f"{URL}/v1/audit-intents/write",
            {
                "json": {"audit_intent_id": str(INTENT)},
                "headers": {"Authorization": "Bearer token"},
                "timeout": 10.0,
                "allow_redirects": False,
            },
        )
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://audit.example.run.app",
        "https://user@audit.example.run.app",
        "https://audit.example.run.app/path",
        "https://audit.example.run.app?target=other",
    ],
)
def test_audit_writer_rejects_non_origin_urls(url):
    with pytest.raises(ValueError):
        AuditWriterClient(url)


def test_audit_writer_requires_exact_success_contract():
    for response in (
        Response(503),
        Response(body={"status": "written", "audit_event_id": "invalid"}),
        Response(
            body={
                "status": "written",
                "audit_event_id": str(EVENT),
                "tenant_id": str(EVENT),
            }
        ),
    ):
        writer = AuditWriterClient(
            URL,
            token_provider=lambda audience: "token",
            session=Session(response),
        )
        assert not writer.write(INTENT)
