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
    if google_workspace_verification is not None:
        verification = TaskRoute(
            "google.workspace.connection.verify",
            "google.workspace.connection.verify",
            google_workspace_verification,
        )
        routes[verification.purpose] = verification
    return routes
