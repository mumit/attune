"""``attune brief`` — one brief, in the terminal (roadmap prompt 08).

The "try it before wiring any chat app" moment: assemble a single morning
brief from the real inbox/calendar and print it. ``--post`` additionally
posts it through the full runtime (channels, memory-informed prep) — the
plain form deliberately builds only connector + client, no Mem0, no
checkpointer, so it works before the memory substrate is even running.
"""

from __future__ import annotations

from typing import Any, Callable


def run_brief(
    *,
    post: bool = False,
    out: Callable[[str], None] = print,
    build: Callable[[], tuple[Any, Any, Any]] | None = None,
    runtime_factory: Callable[[], Any] | None = None,
) -> int:
    """Assemble and print one brief. ``build`` returns (connector, client,
    settings) and is injectable for tests; ``runtime_factory`` likewise for
    the --post path."""
    if post:
        from ..runtime import build_runtime

        runtime = (runtime_factory or build_runtime)()
        brief = runtime.post_brief()
        out(_render(brief))
        out("\n(posted to configured channels)")
        return 0

    connector, client, settings = (build or _default_build)()
    from ..brief import assemble_brief

    user = settings.user_id
    brief = assemble_brief(
        connector,
        client,
        user_id=user,
        user_email=user if "@" in user else None,
        tz=settings.timezone,
    )
    out(_render(brief))
    return 0


def _render(brief: Any) -> str:
    lines = [
        f"Morning brief — {brief.unread_count} unread · "
        f"{brief.event_count} events (times in {brief.timezone})",
    ]
    # Phase 2 stage 2 (docs/future-state.md, G11): the brief leads with the
    # ranked cross-source spine; the model summary and per-source drill-downs
    # (waiting-on here, the rest inside the summary/channel post) follow.
    if brief.spine:
        lines += ["", "What matters now:"]
        lines += [f"  {line}" for line in brief.spine]
    # Phase 3 stage 3 (G11): the pending-approval tally, and — only when a
    # snapshot store was threaded through (the daily posted-brief path; the
    # plain preview never gets one) — the "since yesterday" section.
    if getattr(brief, "pending_tally", None):
        lines += [f"  {brief.pending_tally}"]
    if getattr(brief, "since_yesterday", None):
        lines += ["", "Since yesterday:"]
        lines += [f"  - {line}" for line in brief.since_yesterday]
    lines += ["", brief.summary]
    if brief.waiting_on:
        lines += ["", "Waiting on:"]
        for t in brief.waiting_on:
            age = (
                f" ({(brief.generated_at - t.last_message_at).days}d)"
                if t.last_message_at is not None else ""
            )
            lines.append(f"  - {t.subject}{age}")
    return "\n".join(lines)


def _default_build():  # pragma: no cover - needs live credentials
    from ..config import Settings, WorkspaceBackend
    from ..connectors import make_connector
    from ..credentials import load_google_credentials
    from ..llm import make_client

    settings = Settings.from_env()
    credentials = (
        load_google_credentials(settings)
        if settings.workspace_backend == WorkspaceBackend.GOOGLE_OAUTH
        else None
    )
    connector = make_connector(settings, credentials=credentials)
    return connector, make_client(settings=settings), settings
