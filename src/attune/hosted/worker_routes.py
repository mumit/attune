"""Registered deterministic hosted worker capabilities."""

from __future__ import annotations

from .repositories import HostedJob
from .tenant import TenantContext
from .worker_dispatch import JobExecutor, TaskRoute


def platform_smoke(context: TenantContext, job: HostedJob) -> None:
    if not isinstance(context, TenantContext):
        raise TypeError("verified tenant context is required")
    if job.payload != {"probe": "dispatch-v1"}:
        raise ValueError("platform smoke payload does not match the contract")


def registered_routes(
    *,
    google_gmail_profile: JobExecutor | None = None,
    google_workspace_verification: JobExecutor | None = None,
    google_gmail_draft_create: JobExecutor | None = None,
    google_chat_conversation: JobExecutor | None = None,
    slack_conversation: JobExecutor | None = None,
    web_conversation: JobExecutor | None = None,
    hosted_brief: JobExecutor | None = None,
) -> dict[str, TaskRoute]:
    smoke = TaskRoute("platform.smoke", "platform.smoke", platform_smoke)
    routes = {smoke.purpose: smoke}
    if google_gmail_profile is not None:
        profile = TaskRoute(
            "google.gmail.profile.read",
            "google.gmail.profile.read",
            google_gmail_profile,
        )
        routes[profile.purpose] = profile
    if google_gmail_draft_create is not None:
        draft_create = TaskRoute(
            "google.gmail.draft.create",
            "google.gmail.draft.create",
            google_gmail_draft_create,
        )
        routes[draft_create.purpose] = draft_create
    if google_workspace_verification is not None:
        verification = TaskRoute(
            "google.workspace.connection.verify",
            "google.workspace.connection.verify",
            google_workspace_verification,
        )
        routes[verification.purpose] = verification
    if google_chat_conversation is not None:
        conversation = TaskRoute(
            "channel.google_chat.converse",
            "assistant.conversation.read",
            google_chat_conversation,
        )
        routes[conversation.purpose] = conversation
    if slack_conversation is not None:
        conversation = TaskRoute(
            "channel.slack.converse",
            "assistant.conversation.read",
            slack_conversation,
        )
        routes[conversation.purpose] = conversation
    if web_conversation is not None:
        conversation = TaskRoute(
            "channel.web.converse",
            "assistant.conversation.read",
            web_conversation,
        )
        routes[conversation.purpose] = conversation
    if hosted_brief is not None:
        brief = TaskRoute(
            "channel.brief.deliver",
            "assistant.brief.deliver",
            hosted_brief,
        )
        routes[brief.purpose] = brief
    return routes
