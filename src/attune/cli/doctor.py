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
    "source-channels", "mail-labels", "calendar-writes", "mail-send",
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
    import os

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
        elif (
            settings.google_credentials_file
            and os.path.abspath(settings.chat_credentials_file)
            == os.path.abspath(settings.google_credentials_file)
        ):
            # design.md rule 4: the Chat app identity (service account OR,
            # per credentials.py, an OAuth user credential for orgs that
            # disallow service-account keys) must never be the same
            # credential as the principal's Gmail/Calendar OAuth grant.
            errors.append(
                "ATTUNE_CHAT_CREDENTIALS_FILE must not be the same file as "
                "ATTUNE_GOOGLE_CREDENTIALS_FILE — the Chat app identity must "
                "be a separate credential (a distinct service account, or a "
                "distinct OAuth user credential); see "
                "docs/deployment.md's Google Chat section"
            )
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


def check_mail_send(settings) -> tuple[str, str]:
    """Validate the opt-in SEND_REPLY write path (Phase 4 stage 2, G15):
    mirrors :func:`check_mail_labels`/:func:`check_calendar_writes` exactly
    — a deployment that flips ``ATTUNE_MAIL_SEND_ENABLED`` on a backend
    that structurally cannot send would otherwise silently never send an
    autonomous reply — fail fast instead."""
    if not settings.mail_send_enabled:
        return SKIP, "ATTUNE_MAIL_SEND_ENABLED=0 (sending disabled)"

    from ..config import WorkspaceBackend

    if settings.workspace_backend == WorkspaceBackend.MCP:
        return FAIL, (
            "MCP backend cannot send mail (contract v1 deliberately has no "
            "send tool — see docs/mcp-contract.md); disable "
            "ATTUNE_MAIL_SEND_ENABLED or set ATTUNE_WORKSPACE_BACKEND=google_oauth"
        )
    return PASS, (
        "google_oauth backend supports sending (requires the gmail.send "
        "scope on the authorized credential, AND an explicit send_reply "
        "autonomy grant via `attune autonomy grant`)"
    )


def _fail_workspace(exc: Exception, *, mcp: bool) -> tuple[str, str]:
    """Render a workspace-check FAIL with the inline fix hint from
    ``docs/install/self-hosted.md``'s common-failures table (UX item #4, G20),
    plus the 7-day Testing-token hint when the failure is an
    ``invalid_grant`` (Deliverable B item 2)."""
    base = f"{type(exc).__name__}: {exc}"
    if mcp:
        hint = (
            "check TLS/network/token settings and ensure tools/list includes "
            "every tool in docs/mcp-contract.md"
        )
    else:
        hint = (
            "point ATTUNE_GOOGLE_CREDENTIALS_FILE at the authorized-user JSON "
            "produced by `attune init`, not the downloaded client JSON"
        )
    message = f"{base} — {hint}"
    if "invalid_grant" in str(exc):
        message += (
            "; if your OAuth consent screen is in Testing mode, refresh "
            "tokens expire after 7 days — re-run `attune init` to "
            "re-authorize, and consider `attune init --google-setup` step 5"
        )
    return FAIL, message


def _fail_read(exc: Exception, *, source: str) -> tuple[str, str]:
    """Render a gmail-read/calendar-read FAIL with the common-failures table
    hint, plus the 7-day Testing-token hint on ``invalid_grant``."""
    base = f"{type(exc).__name__}: {exc}"
    message = (
        f"{base} — enable the {source} API, add the test user, include the "
        "required scopes, then rerun `attune init` to authorize again"
    )
    if "invalid_grant" in str(exc):
        message += (
            "; if your OAuth consent screen is in Testing mode, refresh "
            "tokens expire after 7 days — re-run `attune init` to "
            "re-authorize, and consider `attune init --google-setup` step 5"
        )
    return FAIL, message


def check_google_oauth_app(settings) -> tuple[str, str]:
    """WARN about the 7-day Testing-mode refresh-token trap (UX item #2,
    G20). ``attune init --google-setup`` records whether the OAuth consent
    screen is Internal or External+Testing in secret-free setup state
    (never in .env, see ``google_setup_state.py``). External+Testing
    refresh tokens for these scopes typically expire about a week after
    issuance — fine for a smoke test, not an always-on service. See
    docs/install/google-workspace-oauth.md."""
    import os

    from ..config import WorkspaceBackend

    if settings.workspace_backend == WorkspaceBackend.MCP:
        return SKIP, "workspace backend is mcp; no Google consent screen to check"

    from .google_setup_state import GoogleSetupState, google_setup_state_path
    from .setup_state import SetupStateError

    data_dir = settings.data_dir or "."
    path = google_setup_state_path(data_dir)
    if not os.path.exists(path):
        return SKIP, (
            "no recorded consent-screen state (legacy setup; run "
            "`attune init --google-setup` to record one)"
        )
    try:
        state = GoogleSetupState.load_or_create(path)
    except SetupStateError as exc:
        return SKIP, f"could not read consent-screen state: {exc}"

    if state.consent_mode != "external_testing":
        mode = state.consent_mode or "not recorded"
        return SKIP, f"consent screen recorded as {mode}"

    age_note = ""
    cred_file = settings.google_credentials_file
    if cred_file and os.path.exists(cred_file):
        import time

        age_days = (time.time() - os.path.getmtime(cred_file)) / 86400
        age_note = f"; authorized-user file is ~{age_days:.1f} day(s) old (mtime, approximate)"
    return WARN, (
        "OAuth consent screen is External+Testing: refresh tokens for these "
        "scopes typically expire ~7 days after issuance" + age_note + ". Fix: "
        "switch the consent screen to Internal (Workspace accounts) or "
        "publish the app; see docs/install/google-workspace-oauth.md"
    )


def check_data_dir(settings) -> tuple[str, str]:
    """Fatal check for security finding F5 (Low, docs/current-state.md's
    2026-07-18 review): with ``ATTUNE_DATA_DIR`` unset, ``config._path``
    falls back to ``./{filename}`` under whatever umask the process happens
    to run with — the conversation-state JSONL, the audit log, and every
    other state file would land in the current working directory,
    potentially world-readable. FAIL fast rather than let a deployment
    silently write sensitive state somewhere unexpected; PASS only once a
    real directory is configured, and (mirroring ``attune init``'s own
    behavior) verify/correct its permissions to 0700 — owner-only, since
    this directory holds credentials, memory, and audit data.

    This is one of :data:`FATAL_CHECKS`, so ``attune run``'s preflight
    (``run_cmd.run_run`` calling ``run_doctor(fatal_only=True)``) inherits
    the same fail-closed behavior without any additional wiring."""
    import os

    if not settings.data_dir:
        return FAIL, (
            "ATTUNE_DATA_DIR is not set — state files (conversation text, "
            "audit log, credentials) would fall back to the current "
            "working directory with the process's default umask; set "
            "ATTUNE_DATA_DIR to an owner-only directory (`attune init` "
            "prompts for one and creates it with 0700)"
        )
    target = settings.data_dir
    probe = os.path.join(target, ".attune-doctor-probe")
    try:
        os.makedirs(target, mode=0o700, exist_ok=True)
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
    except OSError as exc:
        return FAIL, f"{target} not writable ({exc}) — set ATTUNE_DATA_DIR"
    try:
        os.chmod(target, 0o700)
    except OSError:
        # Windows and unusual filesystems may not support POSIX modes —
        # same tolerance as init_cmd.py's identical chmod.
        pass
    return PASS, f"{target} (permissions verified/corrected to 0700)"


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
            try:
                caller = make_mcp_caller(settings)
                for server, expected in MCP_REQUIRED_TOOLS.items():
                    missing = expected - caller.list_tools(server)
                    if missing:
                        return FAIL, (
                            f"{server} MCP server missing tools: "
                            f"{', '.join(sorted(missing))} — check TLS/network/"
                            "token settings and ensure tools/list includes "
                            "every tool in docs/mcp-contract.md"
                        )
            except Exception as exc:  # noqa: BLE001
                return _fail_workspace(exc, mcp=True)
            return PASS, "MCP Gmail + Calendar capabilities available"
        from ..credentials import load_google_credentials
        try:
            load_google_credentials(settings)
        except Exception as exc:  # noqa: BLE001
            return _fail_workspace(exc, mcp=False)
        return PASS, settings.google_credentials_file or "Application Default Credentials"

    def check_gmail_read() -> tuple[str, str]:
        from ..config import WorkspaceBackend
        if settings.workspace_backend == WorkspaceBackend.MCP:
            return SKIP, "MCP capability checked by workspace row"
        from googleapiclient.discovery import build

        from ..credentials import load_google_credentials

        try:
            service = build(
                "gmail", "v1", credentials=load_google_credentials(settings)
            )
            profile = service.users().getProfile(userId="me").execute()
        except Exception as exc:  # noqa: BLE001
            return _fail_read(exc, source="Gmail")
        return PASS, profile.get("emailAddress", "ok")

    def check_calendar_read() -> tuple[str, str]:
        from ..config import WorkspaceBackend
        if settings.workspace_backend == WorkspaceBackend.MCP:
            return SKIP, "MCP capability checked by workspace row"
        from googleapiclient.discovery import build

        from ..credentials import load_google_credentials

        try:
            service = build(
                "calendar", "v3", credentials=load_google_credentials(settings)
            )
            # calendar.events authorizes the runtime's events.list/insert
            # calls but not calendars.get metadata. Test the capability we
            # actually use.
            service.events().list(
                calendarId=settings.calendar_id,
                maxResults=1,
                singleEvents=True,
            ).execute()
        except Exception as exc:  # noqa: BLE001
            return _fail_read(exc, source="Calendar")
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

        try:
            resp = WebClient(token=settings.slack_bot_token).auth_test()
        except Exception as exc:  # noqa: BLE001
            return FAIL, (
                f"{type(exc).__name__}: {exc} — add the required Slack "
                "scopes above and reinstall the app"
            )
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
        Check("data-dir", lambda: check_data_dir(settings)),
        Check("llm", check_llm),
        Check("workspace", check_workspace),
        Check("google-oauth-app", lambda: check_google_oauth_app(settings)),
        Check("channels", lambda: check_channel_routes(settings)),
        Check("source-channels", lambda: check_source_channels(settings)),
        Check("mail-labels", lambda: check_mail_labels(settings)),
        Check("calendar-writes", lambda: check_calendar_writes(settings)),
        Check("mail-send", lambda: check_mail_send(settings)),
        Check("audit-chain", lambda: check_audit_chain(settings)),
        Check("gmail-read", check_gmail_read),
        Check("calendar-read", check_calendar_read),
        Check("qdrant", check_qdrant),
        Check("slack", check_slack),
        Check("pubsub", check_pubsub),
    ]
