from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from attune.hosted.repositories import HostedJob
from attune.hosted.tenant import TenantContext
from attune.hosted.worker_routes import platform_smoke, registered_routes


def job(payload):
    return HostedJob(
        UUID("10000000-0000-4000-8000-000000000711"),
        "platform.smoke",
        "leased",
        "platform.smoke",
        payload,
        1,
        datetime.now(timezone.utc),
        datetime.now(timezone.utc),
    )


def test_only_registered_smoke_route_has_no_external_effect_arguments():
    routes = registered_routes()
    assert set(routes) == {"platform.smoke"}
    route = routes["platform.smoke"]
    assert route.capability == "platform.smoke"
    platform_smoke(
        TenantContext(UUID("10000000-0000-4000-8000-000000000712")),
        job({"probe": "dispatch-v1"}),
    )
    with pytest.raises(ValueError):
        platform_smoke(
            TenantContext(UUID("10000000-0000-4000-8000-000000000712")),
            job({"url": "https://untrusted.example"}),
        )


def test_google_profile_route_is_registered_only_with_explicit_executor():
    calls = []
    routes = registered_routes(google_gmail_profile=lambda *args: calls.append(args))
    assert set(routes) == {"platform.smoke", "google.gmail.profile.read"}
    route = routes["google.gmail.profile.read"]
    assert route.capability == "google.gmail.profile.read"
    context = TenantContext(UUID("10000000-0000-4000-8000-000000000712"))
    profile_job = job({"connector_id": "10000000-0000-4000-8000-000000000713"})
    route.execute(context, profile_job)
    assert calls == [(context, profile_job)]


def test_gmail_draft_create_route_requires_explicit_executor():
    calls = []
    assert "google.gmail.draft.create" not in registered_routes()
    routes = registered_routes(
        google_gmail_draft_create=lambda *args: calls.append(args)
    )
    assert set(routes) == {"platform.smoke", "google.gmail.draft.create"}
    route = routes["google.gmail.draft.create"]
    assert route.capability == "google.gmail.draft.create"
    context = TenantContext(UUID("10000000-0000-4000-8000-000000000716"))
    draft_job = job({"thread_ref": "thread_1"})
    route.execute(context, draft_job)
    assert calls == [(context, draft_job)]


def test_workspace_verification_route_requires_explicit_executor():
    calls = []
    routes = registered_routes(
        google_workspace_verification=lambda *args: calls.append(args)
    )
    assert set(routes) == {"platform.smoke", "google.workspace.connection.verify"}
    route = routes["google.workspace.connection.verify"]
    assert route.capability == "google.workspace.connection.verify"


def test_google_chat_conversation_route_requires_explicit_executor():
    calls = []
    routes = registered_routes(
        google_chat_conversation=lambda *args: calls.append(args)
    )
    route = routes["channel.google_chat.converse"]
    assert route.capability == "assistant.conversation.read"


def test_slack_conversation_route_requires_explicit_executor():
    calls = []
    assert "channel.slack.converse" not in registered_routes()
    routes = registered_routes(
        slack_conversation=lambda *args: calls.append(args)
    )
    route = routes["channel.slack.converse"]
    assert route.capability == "assistant.conversation.read"


def test_web_conversation_route_requires_explicit_executor():
    calls = []
    assert "channel.web.converse" not in registered_routes()
    routes = registered_routes(
        web_conversation=lambda *args: calls.append(args)
    )
    route = routes["channel.web.converse"]
    assert route.capability == "assistant.conversation.read"
    context = TenantContext(UUID("10000000-0000-4000-8000-000000000714"))
    conversation_job = job({"conversation_id": "10000000-0000-4000-8000-000000000715"})
    route.execute(context, conversation_job)
    assert calls == [(context, conversation_job)]


def test_hosted_brief_route_requires_explicit_executor():
    """Gate-off pin (Phase 5 stage 4, G12): with no executor supplied, the
    brief route is absent, exactly like every other optional route."""
    calls = []
    assert "channel.brief.deliver" not in registered_routes()
    routes = registered_routes(hosted_brief=lambda *args: calls.append(args))
    assert set(routes) == {"platform.smoke", "channel.brief.deliver"}
    route = routes["channel.brief.deliver"]
    assert route.capability == "assistant.brief.deliver"
    context = TenantContext(UUID("10000000-0000-4000-8000-000000000717"))
    brief_job = job({"principal_id": "10000000-0000-4000-8000-000000000718"})
    route.execute(context, brief_job)
    assert calls == [(context, brief_job)]
