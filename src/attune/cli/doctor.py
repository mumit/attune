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
    "installation", "env", "data-dir", "llm", "workspace"
)


@dataclass
class Check:
    name: str
    fn: CheckFn


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
            from ..connectors.mcp import CALENDAR_SERVER, GMAIL_SERVER
            from ..connectors.mcp_client import make_mcp_caller
            caller = make_mcp_caller(settings)
            required = {
                GMAIL_SERVER: {"search_threads", "get_thread", "create_draft"},
                CALENDAR_SERVER: {"list_events", "get_event"},
            }
            for server, expected in required.items():
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
        host = os.environ.get("ATTUNE_QDRANT_HOST", "localhost")
        port = int(os.environ.get("ATTUNE_QDRANT_PORT", "6333"))
        url = f"http://{host}:{port}/readyz"
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
        Check("gmail-read", check_gmail_read),
        Check("calendar-read", check_calendar_read),
        Check("qdrant", check_qdrant),
        Check("slack", check_slack),
        Check("pubsub", check_pubsub),
    ]
