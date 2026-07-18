"""``attune doctor`` — read-only validation with actionable fix hints
(roadmap prompt 08).

One PASS/FAIL/SKIP line per check; exit code 1 if anything FAILs. Every
check is an injected zero-arg callable returning ``(status, detail)``, so
tests fake the whole battery — the default battery does the real (read-only)
work with lazy imports and turns every exception into a FAIL with a hint
rather than a traceback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
WARN = "WARN"

CheckFn = Callable[[], tuple[str, str]]

# Checks that must pass before `attune run` will start (see run_cmd.py).
FATAL_CHECKS = (
    "installation", "env", "data-dir", "llm", "workspace", "channels",
    "source-channels", "mail-labels", "calendar-writes",
)


@dataclass
class Check:
    name: str
    fn: CheckFn


def _qdrant_ready_url(settings) -> str:
    """The exact server target shared with Mem0 through typed settings."""
    return f"http://{settings.qdrant_host}:{settings.qdrant_port}/readyz"


def check_channel_routes(settings) -> tuple[str, str]:
    """Validate that every selected route has usable local configuration."""
    routed = (
        settings.brief_channels
        | settings.notification_channels
        | settings.interaction_channels
        | ({settings.approval_channel} if settings.approval_channel else set())
    )
    if not routed:
        return SKIP, "no Slack or Google Chat routes configured"

    errors: list[str] = []
    slack_proactive = (
        "slack" in settings.brief_channels
        or "slack" in settings.notification_channels
        or settings.approval_channel == "slack"
    )
    if "slack" in routed:
        if not settings.slack_bot_token:
            errors.append("Slack routes require SLACK_BOT_TOKEN")
        if slack_proactive and not settings.slack_default_channel:
            errors.append("proactive Slack routes require ATTUNE_SLACK_CHANNEL")
        if "slack" in settings.interaction_channels:
            if not settings.slack_app_token:
                errors.append("Slack interactions require SLACK_APP_TOKEN")
            if not settings.slack_allowed_users:
                errors.append("Slack interactions require ATTUNE_SLACK_ALLOWED_USERS")

    if "google_chat" in routed:
        if not settings.chat_default_space:
            errors.append("Google Chat routes require ATTUNE_CHAT_SPACE")
        if not settings.chat_credentials_file:
            errors.append("Google Chat routes require ATTUNE_CHAT_CREDENTIALS_FILE")
        if (
            "google_chat" in settings.interaction_channels
            and not settings.chat_allowed_users
        ):
            errors.append(
                "Google Chat interactions require ATTUNE_CHAT_ALLOWED_USERS"
            )
        if (
            settings.approval_channel == "google_chat"
            and not settings.chat_interaction_pubsub_subscription
        ):
            errors.append(
                "Google Chat approvals require "
                "ATTUNE_CHAT_INTERACTION_PUBSUB_SUBSCRIPTION"
            )

        from ..config import IngestionMode, WorkspaceBackend

        if "google_chat" in settings.interaction_channels:
            if (
                settings.workspace_backend == WorkspaceBackend.MCP
                and not settings.chat_interaction_pubsub_subscription
            ):
                errors.append(
                    "Google Chat interaction with MCP requires the verified "
                    "Chat interaction Pub/Sub subscription"
                )
            elif (
                settings.ingestion_mode == IngestionMode.GOOGLE_PUBSUB
                and not (
                    settings.chat_pubsub_subscription
                    or settings.chat_interaction_pubsub_subscription
                )
            ):
                errors.append(
                    "Google Chat interaction in google_pubsub mode requires "
                    "a Chat Pub/Sub subscription"
                )

    if errors:
        return FAIL, "; ".join(dict.fromkeys(errors))
    return PASS, "configured routes have credentials, destinations, and allowlists"


def check_source_channels(settings) -> tuple[str, str]:
    """Validate opt-in Slack/Chat SOURCE ingestion (Phase 2 stage 1 of
    ``docs/future-state.md``, gaps G1/G3): a configured source with no way
    to read it would otherwise be a silent no-op — same fail-fast posture as
    :func:`check_channel_routes` for the conversational routes, and
    deliberately a SEPARATE check: source ingestion and the interaction
    allowlists it's unrelated to (see ``ingestion/sources.py``) can be
    configured entirely independently of each other."""
    if not settings.slack_source_channels and not settings.chat_source_spaces:
        return SKIP, "no Slack/Chat source channels or spaces configured"

    errors: list[str] = []
    if settings.slack_source_channels and not settings.slack_bot_token:
        errors.append("ATTUNE_SLACK_SOURCE_CHANNELS requires SLACK_BOT_TOKEN")
    if settings.chat_source_spaces and not settings.chat_credentials_file:
        errors.append(
            "ATTUNE_CHAT_SOURCE_SPACES requires ATTUNE_CHAT_CREDENTIALS_FILE"
        )
    if errors:
        return FAIL, "; ".join(errors)
    return PASS, (
        f"{len(settings.slack_source_channels)} Slack channel(s), "
        f"{len(settings.chat_source_spaces)} Chat space(s) configured"
    )


def check_mail_labels(settings) -> tuple[str, str]:
    """Validate the opt-in archive/label write path (Phase 3 stage 1, G9): a
    deployment that flips ``ATTUNE_MAIL_LABELS_ENABLED`` on a backend that
    structurally cannot label threads would otherwise silently never
    propose archive actions — fail fast instead, same posture as
    :func:`check_source_channels` for opt-in source ingestion."""
    if not settings.mail_labels_enabled:
        return SKIP, "ATTUNE_MAIL_LABELS_ENABLED=0 (mail labeling disabled)"

    from ..config import WorkspaceBackend

    if settings.workspace_backend == WorkspaceBackend.MCP:
        return FAIL, (
            "MCP backend cannot label/archive threads (contract v1 has no "
            "label-removal tool — see docs/mcp-contract.md); disable "
            "ATTUNE_MAIL_LABELS_ENABLED or set ATTUNE_WORKSPACE_BACKEND=google_oauth"
        )
    return PASS, (
        "google_oauth backend supports thread labeling (requires the "
        "gmail.modify scope on the authorized credential)"
    )


def check_calendar_writes(settings) -> tuple[str, str]:
    """Validate the opt-in decline-invite/reschedule write path (Phase 3
    stage 2), mirroring :func:`check_mail_labels` exactly: a deployment that
    flips ``ATTUNE_CALENDAR_WRITES_ENABLED`` on a backend that structurally
    cannot write to Calendar would otherwise silently never propose a
    decline or reschedule — fail fast instead."""
    if not settings.calendar_writes_enabled:
        return SKIP, "ATTUNE_CALENDAR_WRITES_ENABLED=0 (calendar writes disabled)"

    from ..config import WorkspaceBackend

    if settings.workspace_backend == WorkspaceBackend.MCP:
        return FAIL, (
            "MCP backend cannot decline invites or reschedule events "
            "(contract v1 has neither tool — see docs/mcp-contract.md); "
            "disable ATTUNE_CALENDAR_WRITES_ENABLED or set "
            "ATTUNE_WORKSPACE_BACKEND=google_oauth"
        )
    return PASS, (
        "google_oauth backend supports calendar writes (requires the "
        "calendar.events scope on the authorized credential)"
    )


def check_audit_chain(settings) -> tuple[str, str]:
    """Verify the local JSONL audit log's hash chain (security finding F1).

    ``grants.py``'s ``track_records``/``suggest_graduations`` fold this file
    into autonomy-graduation decisions, so a silently edited or deleted line
    would skew them without anyone noticing. This walks the chain the same
    way :meth:`JsonlAuditLog.verify` does and reports the first place it
    breaks, rather than letting Doctor's battery pass over a tampered file.
    """
    import os

    from ..audit.log import JsonlAuditLog

    path = settings.audit_log_path
    if not os.path.exists(path):
        return SKIP, f"{path} does not exist yet"

    result = JsonlAuditLog(path).verify()
    if not result.ok:
        return FAIL, f"line {result.first_bad_line}: {result.reason}"
    return PASS, (
        f"{result.checked} hashed, {result.legacy} legacy line(s), chain intact"
    )


def run_doctor(
    checks: list[Check] | None = None,
    *,
    out: Callable[[str], None] = print,
    fatal_only: bool = False,
) -> int:
    """Run the battery, print one line per check, return the exit code."""
    checks = checks if checks is not None else build_checks()
    if fatal_only:
        checks = [c for c in checks if c.name in FATAL_CHECKS]

    failed = 0
    warned = 0
    for check in checks:
        try:
            status, detail = check.fn()
        except Exception as exc:  # noqa: BLE001 — a crashing check is a FAIL, not a traceback
            status, detail = FAIL, f"check crashed: {type(exc).__name__}: {exc}"
        if status == FAIL:
            failed += 1
        elif status == WARN:
            warned += 1
        out(f"{status:4}  {check.name:22} {detail}")

    out("")
    if failed:
        out(f"{failed} check(s) FAILED.")
    elif warned:
        out(f"All required checks passed; {warned} warning(s) remain.")
    else:
        out("All checks passed.")
    return 0 if failed == 0 else 1


# --- the default battery -----------------------------------------------------


def build_checks() -> list[Check]:  # pragma: no cover - thin assembly; each
    # check is exercised through run_doctor with injected fakes, and the real
    # ones need live services by definition.
    import os
    import sys
    import warnings

    # Replace google-api-core's import-time wall of text with one concise,
    # actionable Doctor row below.
    warnings.filterwarnings(
        "ignore",
        message=r"You are using a Python version .*",
        category=FutureWarning,
        module=r"google\.api_core\._python_version_support",
    )

    from ..config import Settings

    try:
        settings = Settings.from_env()
    except Exception as exc:  # noqa: BLE001
        # Everything downstream needs settings; report the one failure.
        msg = f"{type(exc).__name__}: {exc} — fix the ATTUNE_* variable it names"
        return [Check("env", lambda: (FAIL, msg))]

    def check_installation() -> tuple[str, str]:
        import attune

        package_file = os.path.realpath(attune.__file__ or "")
        checkout = os.path.join(os.path.realpath(os.getcwd()), "src", "attune")
        if os.path.isdir(checkout) and not package_file.startswith(
            checkout + os.sep
        ):
            return FAIL, (
                f"this checkout is using {package_file} — reinstall with "
                "pip install -e ."
            )
        return PASS, package_file

    def check_python() -> tuple[str, str]:
        version = ".".join(str(part) for part in sys.version_info[:3])
        if sys.version_info < (3, 11):
            return WARN, (
                f"{version} works now; use Python 3.12 before Google library "
                "support for 3.10 ends on 2026-10-04"
            )
        return PASS, version

    def check_env() -> tuple[str, str]:
        settings.validate()
        return PASS, f"workspace={settings.workspace_backend.value}, ingestion={settings.ingestion_mode.value}"

    def check_data_dir() -> tuple[str, str]:
        target = settings.data_dir or "."
        probe = os.path.join(target, ".attune-doctor-probe")
        try:
            os.makedirs(target, exist_ok=True)
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
        except OSError as exc:
            return FAIL, f"{target} not writable ({exc}) — set ATTUNE_DATA_DIR"
        return PASS, target

    def check_llm() -> tuple[str, str]:
        if not settings.llm_api_key:
            return FAIL, "ATTUNE_LLM_API_KEY not set — add it to .env"
        from ..llm import Task, make_client, model_for

        model = "client construction"
        try:
            client = make_client(settings=settings)
            models = dict.fromkeys(model_for(task, settings) for task in Task)
            for model in models:
                client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "Reply OK."}],
                    max_tokens=2,
                )
        except Exception as exc:  # noqa: BLE001
            return FAIL, (
                f"gateway/model {model!r} unavailable: {type(exc).__name__} — set the "
                "matching ATTUNE_MODEL_* override to a model your token can use"
            )
        return PASS, f"{settings.llm_base_url}; {len(models)} routed model(s) accepted"

    def check_workspace() -> tuple[str, str]:
        from ..config import WorkspaceBackend
        if settings.workspace_backend == WorkspaceBackend.MCP:
            from ..connectors.mcp import MCP_REQUIRED_TOOLS
            from ..connectors.mcp_client import make_mcp_caller
            caller = make_mcp_caller(settings)
            for server, expected in MCP_REQUIRED_TOOLS.items():
                missing = expected - caller.list_tools(server)
                if missing:
                    return FAIL, f"{server} MCP server missing tools: {', '.join(sorted(missing))}"
            return PASS, "MCP Gmail + Calendar capabilities available"
        from ..credentials import load_google_credentials
        load_google_credentials(settings)
        return PASS, settings.google_credentials_file or "Application Default Credentials"

    def check_gmail_read() -> tuple[str, str]:
        from ..config import WorkspaceBackend
        if settings.workspace_backend == WorkspaceBackend.MCP:
            return SKIP, "MCP capability checked by workspace row"
        from googleapiclient.discovery import build

        from ..credentials import load_google_credentials

        service = build(
            "gmail", "v1", credentials=load_google_credentials(settings)
        )
        profile = service.users().getProfile(userId="me").execute()
        return PASS, profile.get("emailAddress", "ok")

    def check_calendar_read() -> tuple[str, str]:
        from ..config import WorkspaceBackend
        if settings.workspace_backend == WorkspaceBackend.MCP:
            return SKIP, "MCP capability checked by workspace row"
        from googleapiclient.discovery import build

        from ..credentials import load_google_credentials

        service = build(
            "calendar", "v3", credentials=load_google_credentials(settings)
        )
        # calendar.events authorizes the runtime's events.list/insert calls but
        # not calendars.get metadata. Test the capability we actually use.
        service.events().list(
            calendarId=settings.calendar_id,
            maxResults=1,
            singleEvents=True,
        ).execute()
        return PASS, settings.calendar_id

    def check_qdrant() -> tuple[str, str]:
        # Mem0 runs in-process. Its actual external dependency is Qdrant, not
        # the obsolete standalone Mem0 REST endpoint represented by mem0_url.
        host = settings.qdrant_host
        port = settings.qdrant_port
        url = _qdrant_ready_url(settings)
        import urllib.request

        try:
            urllib.request.urlopen(url, timeout=5)
        except Exception as exc:  # noqa: BLE001
            return FAIL, (
                f"{host}:{port} unreachable ({type(exc).__name__}) — "
                "start deploy/compose.yml"
            )
        return PASS, f"{host}:{port}"

    def check_slack() -> tuple[str, str]:
        if not settings.slack_bot_token:
            return SKIP, "SLACK_BOT_TOKEN not set"
        from slack_sdk import WebClient

        resp = WebClient(token=settings.slack_bot_token).auth_test()
        return PASS, resp.get("team", "authenticated")

    def check_pubsub() -> tuple[str, str]:
        subscriptions = [
            s
            for s in (
                settings.gmail_pubsub_subscription,
                settings.chat_pubsub_subscription,
                settings.chat_interaction_pubsub_subscription,
                settings.calendar_pubsub_subscription,
            )
            if s
        ]
        if not subscriptions:
            return SKIP, "no Pub/Sub subscriptions configured (poll mode?)"
        from google.cloud import pubsub_v1

        subscriber = pubsub_v1.SubscriberClient()
        for sub in subscriptions:
            subscriber.get_subscription(request={"subscription": sub})
        return PASS, f"{len(subscriptions)} subscription(s) exist"

    return [
        Check("installation", check_installation),
        Check("python", check_python),
        Check("env", check_env),
        Check("data-dir", check_data_dir),
        Check("llm", check_llm),
        Check("workspace", check_workspace),
        Check("channels", lambda: check_channel_routes(settings)),
        Check("source-channels", lambda: check_source_channels(settings)),
        Check("mail-labels", lambda: check_mail_labels(settings)),
        Check("calendar-writes", lambda: check_calendar_writes(settings)),
        Check("audit-chain", lambda: check_audit_chain(settings)),
        Check("gmail-read", check_gmail_read),
        Check("calendar-read", check_calendar_read),
        Check("qdrant", check_qdrant),
        Check("slack", check_slack),
        Check("pubsub", check_pubsub),
    ]
