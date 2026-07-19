"""``attune init``: create, edit, and migrate an Attune environment file."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Callable

from dotenv import dotenv_values

DEFAULT_DATA_DIR = "~/.attune"

# The documented mixed-provider starting point from docs/configuration.md's
# "Recommended model routing" section (UX item #3, G20). Used only as the
# DEFAULT shown for these questions -- never force-overwritten over an
# already-configured value -- when ``--recommended`` is passed. Keep these
# two literally in sync with configuration.md; a golden test pins them.
RECOMMENDED_MODELS = {
    "ATTUNE_MODEL_DEFAULT": "gpt-5.6-terra",
    "ATTUNE_MODEL_CLASSIFY": "claude-haiku-4-5",
    "ATTUNE_MODEL_DRAFT": "claude-sonnet-5",
    "ATTUNE_MODEL_REASON": "gpt-5.6-terra",
    "ATTUNE_MODEL_CONSOLIDATE": "gpt-5.6-terra",
    "ATTUNE_MODEL_CONVERSE": "claude-sonnet-5",
    "ATTUNE_MODEL_MEMORY_EXTRACT": "claude-haiku-4-5",
}
RECOMMENDED_EMBEDDING_MODEL = "text-embedding-3-small"
RECOMMENDED_EMBEDDING_DIMENSIONS = "1536"


def _external_testing_reminder(data_dir: str) -> str | None:
    """One-line persistent reminder when the checklist recorded
    External+Testing (Deliverable B item 3, G20/UX item #2)."""
    from .google_setup_state import GoogleSetupState, google_setup_state_path
    from .setup_state import SetupStateError

    if not data_dir:
        return None
    path = google_setup_state_path(data_dir)
    if not os.path.exists(path):
        return None
    try:
        state = GoogleSetupState.load_or_create(path)
    except (SetupStateError, OSError, ValueError):
        return None
    if state.consent_mode != "external_testing":
        return None
    return (
        "Reminder: OAuth consent screen is External+Testing — refresh "
        "tokens expire ~7 days after issuance (see `attune doctor` check "
        "google-oauth-app)."
    )

_SPECIAL_MIGRATIONS = {
    "FUELIX_TOKEN": "ATTUNE_LLM_API_KEY",
    "BEARER_OPENAI_TOKEN": "ATTUNE_LLM_API_KEY",
    "ADC_CONNECTOR_MODE": "ATTUNE_WORKSPACE_BACKEND",
}
_DROP_KEYS = {"ADC_DEPLOYMENT", "ATTUNE_DEPLOYMENT"}


def _new_key(key: str) -> str:
    if key in _SPECIAL_MIGRATIONS:
        return _SPECIAL_MIGRATIONS[key]
    if key.startswith("ADC_"):
        return "ATTUNE_" + key[4:]
    return key


def _load_existing(path: str) -> tuple[str, dict[str, str]]:
    if not os.path.exists(path):
        return "", {}
    with open(path) as fh:
        original = fh.read()
    parsed = {k: v or "" for k, v in dotenv_values(path).items() if k}
    migrated: dict[str, str] = {}
    for key, value in parsed.items():
        if key in _DROP_KEYS:
            continue
        new = _new_key(key)
        if new == "ATTUNE_WORKSPACE_BACKEND" and value == "direct_oauth":
            value = "google_oauth"
        if new == "ATTUNE_INGESTION_MODE" and value == "push":
            value = "google_pubsub"
        # A new spelling already in the file wins over its legacy alias.
        if new not in migrated or key == new:
            migrated[new] = value
    return original, migrated


def _env_value(value: str) -> str:
    if value == "":
        return ""
    if re.search(r"\s|#|['\"]", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _rewrite_env(original: str, updates: dict[str, str]) -> str:
    """Patch managed/legacy assignments while preserving unknown lines."""
    assignment = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)=")
    written: set[str] = set()
    output: list[str] = []
    for line in original.splitlines():
        match = assignment.match(line)
        if not match:
            output.append(line)
            continue
        old_key = match.group(2)
        if old_key in _DROP_KEYS:
            continue
        key = _new_key(old_key)
        if key in written:
            continue
        if key in updates:
            output.append(f"{key}={_env_value(updates[key])}")
            written.add(key)
        else:
            output.append(line)

    missing = [key for key in updates if key not in written]
    if missing:
        if output and output[-1] != "":
            output.append("")
        output += ["# --- managed by attune init ---"]
        output += [f"{key}={_env_value(updates[key])}" for key in missing]
    return "\n".join(output).rstrip() + "\n"


def _atomic_write(path: str, content: str, *, backup: bool) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    if backup and os.path.exists(path):
        shutil.copy2(path, path + ".bak")
        os.chmod(path + ".bak", 0o600)
    fd, tmp = tempfile.mkstemp(prefix=".attune-env-", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run_init(
    *,
    env_file: str = ".env",
    fresh: bool = False,
    force: bool = False,  # compatibility with the pre-rename callable
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
    oauth_flow: Callable[..., str] | None = None,
    target: str = "configure",
    yes: bool = False,
    quick: bool = False,
    recommended: bool = False,
    google_setup: Callable[..., object] | None = None,
    local_runner: (
        Callable[
            [list[str] | tuple[str, ...]], subprocess.CompletedProcess[str]
        ]
        | None
    ) = None,
    doctor: Callable[..., int] | None = None,
    out: Callable[[str], None] = print,
) -> int:
    if target not in {"configure", "local"}:
        out(f"Unsupported setup target: {target}")
        return 2
    existed = os.path.exists(env_file)
    original, current = ("", {}) if (fresh or force) else _load_existing(env_file)

    def ask_default(prompt: str, default: str) -> str:
        shown = default or "blank"
        answer = ask(f"{prompt} [{shown}] (Enter keeps, - clears): ").strip()
        return "" if answer == "-" else (answer or default)

    def ask_kept_secret(prompt: str, key: str) -> str:
        existing = current.get(key, "")
        suffix = " [configured; Enter keeps, - clears]" if existing else " [blank to skip]"
        answer = ask_secret(prompt + suffix + ": ")
        if answer == "-":
            return ""
        return answer or existing

    out(("Editing " if existed and not (fresh or force) else "Creating ") + env_file)
    out("Existing values are defaults; secrets are never displayed.")
    if quick:
        out(
            "Quick mode: asking only the essential questions; everything "
            "else keeps its current value or a safe default."
        )

    backend = ask_default(
        "Workspace backend (google_oauth/mcp)",
        current.get("ATTUNE_WORKSPACE_BACKEND", "google_oauth"),
    )
    ingestion = (
        current.get("ATTUNE_INGESTION_MODE", "poll")
        if quick
        else ask_default(
            "Ingestion mode (poll/google_pubsub)",
            current.get("ATTUNE_INGESTION_MODE", "poll"),
        )
    )
    data_dir = os.path.expanduser(
        ask_default("Data directory", current.get("ATTUNE_DATA_DIR", DEFAULT_DATA_DIR))
    )
    if data_dir:
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        # This directory contains credentials, workflow state, memory, and
        # audit data. The explicit Attune data directory is owner-private.
        try:
            os.chmod(data_dir, 0o700)
        except OSError:
            # Windows and unusual filesystems may not support POSIX modes.
            pass
    user_id = ask_default(
        "Google mailbox email / memory principal",
        current.get("ATTUNE_USER_ID", "me"),
    )
    internal_domains = ask_default(
        "Internal email domains (comma-separated)",
        current.get(
            "ATTUNE_INTERNAL_DOMAINS",
            user_id.rsplit("@", 1)[1] if "@" in user_id else "",
        ),
    )

    llm_base = ask_default(
        "OpenAI-compatible base URL",
        current.get("ATTUNE_LLM_BASE_URL", "https://api.openai.com/v1"),
    )
    llm_key = ask_kept_secret("LLM API key / bearer token", "ATTUNE_LLM_API_KEY")
    model_default = ask_default(
        "Default chat model",
        current.get(
            "ATTUNE_MODEL_DEFAULT",
            RECOMMENDED_MODELS["ATTUNE_MODEL_DEFAULT"] if recommended else "",
        ),
    )
    model_prompts = {
        "ATTUNE_MODEL_CLASSIFY": "Classification model",
        "ATTUNE_MODEL_DRAFT": "Drafting model",
        "ATTUNE_MODEL_REASON": "Reasoning model",
        "ATTUNE_MODEL_CONSOLIDATE": "Consolidation model",
        "ATTUNE_MODEL_CONVERSE": "Conversation model",
        "ATTUNE_MODEL_MEMORY_EXTRACT": "Memory extraction model",
    }
    if quick:
        # Task-model overrides are skipped in quick mode; blank means "use
        # ATTUNE_MODEL_DEFAULT" at runtime (llm.py's model_for), unless
        # --recommended fills the documented per-task split instead.
        models = {
            key: current.get(key, RECOMMENDED_MODELS[key] if recommended else "")
            for key in model_prompts
        }
    else:
        models = {
            key: ask_default(
                prompt,
                current.get(
                    key, RECOMMENDED_MODELS[key] if recommended else model_default
                ),
            )
            for key, prompt in model_prompts.items()
        }
    embedding_base = (
        current.get("ATTUNE_EMBEDDING_BASE_URL", llm_base)
        if quick
        else ask_default(
            "Embedding API base URL",
            current.get("ATTUNE_EMBEDDING_BASE_URL", llm_base),
        )
    )
    embedding_key = ask_kept_secret(
        "Embedding API key (blank uses LLM key)", "ATTUNE_EMBEDDING_API_KEY"
    )
    embedding_model = ask_default(
        "Embedding model",
        current.get(
            "ATTUNE_EMBEDDING_MODEL",
            RECOMMENDED_EMBEDDING_MODEL if recommended else "",
        ),
    )
    embedding_dims = (
        current.get(
            "ATTUNE_EMBEDDING_DIMENSIONS",
            RECOMMENDED_EMBEDDING_DIMENSIONS if recommended else "",
        )
        if quick
        else ask_default(
            "Embedding dimensions",
            current.get(
                "ATTUNE_EMBEDDING_DIMENSIONS",
                RECOMMENDED_EMBEDDING_DIMENSIONS if recommended else "",
            ),
        )
    )

    google_project = current.get("GOOGLE_PROJECT_ID", "")
    google_creds = current.get("ATTUNE_GOOGLE_CREDENTIALS_FILE", "")
    mcp_url = current.get("ATTUNE_MCP_URL", "")
    mcp_gmail_url = current.get("ATTUNE_MCP_GMAIL_URL", "")
    mcp_calendar_url = current.get("ATTUNE_MCP_CALENDAR_URL", "")
    mcp_token = current.get("ATTUNE_MCP_TOKEN", "")
    if quick:
        pass  # workspace credentials keep their current values, unasked
    elif backend == "google_oauth":
        google_project = ask_default("Google Cloud project ID", google_project)
        google_creds = _google_credentials_step(
            ask=ask,
            ask_default=ask_default,
            out=out,
            data_dir=data_dir,
            oauth_flow=oauth_flow,
            default=google_creds,
            google_setup=google_setup,
        )
    else:
        mcp_url = ask_default("MCP Streamable HTTP URL", mcp_url)
        if not mcp_url:
            mcp_gmail_url = ask_default("Gmail MCP URL", mcp_gmail_url)
            mcp_calendar_url = ask_default("Calendar MCP URL", mcp_calendar_url)
        mcp_token = ask_kept_secret("MCP bearer token", "ATTUNE_MCP_TOKEN")

    if quick:
        slack_bot = current.get("SLACK_BOT_TOKEN", "")
        slack_app = current.get("SLACK_APP_TOKEN", "")
        slack_channel = current.get("ATTUNE_SLACK_CHANNEL", "")
        slack_allowed = current.get("ATTUNE_SLACK_ALLOWED_USERS", "")
        chat_space = current.get("ATTUNE_CHAT_SPACE", "")
        chat_creds = current.get("ATTUNE_CHAT_CREDENTIALS_FILE", "")
        chat_allowed = current.get("ATTUNE_CHAT_ALLOWED_USERS", "")
        brief_channels = current.get("ATTUNE_BRIEF_CHANNELS", "")
        approval_channel = current.get("ATTUNE_APPROVAL_CHANNEL", "")
        notification_channels = current.get("ATTUNE_NOTIFICATION_CHANNELS", "")
        interaction_channels = current.get("ATTUNE_INTERACTION_CHANNELS", "")
        visibility_ack = current.get("ATTUNE_ACK_DESTINATION_VISIBILITY", "")
        timezone = current.get("ATTUNE_TIMEZONE", "UTC")
        brief_time = current.get("ATTUNE_BRIEF_TIME", "07:30")
    else:
        slack_bot = ask_kept_secret("Slack bot token", "SLACK_BOT_TOKEN")
        slack_app = (
            ask_kept_secret("Slack app-level token", "SLACK_APP_TOKEN")
            if slack_bot else ""
        )
        slack_channel = ask_default(
            "Slack destination ID (owner U... or conversation D/C/G...)",
            current.get("ATTUNE_SLACK_CHANNEL", ""),
        ) if slack_bot else ""
        slack_allowed = ask_default(
            "Allowed Slack user IDs", current.get("ATTUNE_SLACK_ALLOWED_USERS", "")
        ) if slack_bot else ""

        chat_space = ask_default(
            "Google Chat space (spaces/..., blank to skip)",
            current.get("ATTUNE_CHAT_SPACE", ""),
        )
        chat_creds = ask_default(
            "Google Chat app service-account JSON",
            current.get("ATTUNE_CHAT_CREDENTIALS_FILE", ""),
        ) if chat_space else ""
        chat_allowed = ask_default(
            "Allowed Google Chat user IDs", current.get("ATTUNE_CHAT_ALLOWED_USERS", "")
        ) if chat_space else ""

        available = [
            name for name, ok in (("slack", slack_bot), ("google_chat", chat_space)) if ok
        ]
        route_default = ",".join(available)
        brief_channels = ask_default(
            "Brief channels", current.get("ATTUNE_BRIEF_CHANNELS", route_default)
        )
        approval_channel = ask_default(
            "Approval channel",
            current.get("ATTUNE_APPROVAL_CHANNEL", available[0] if available else ""),
        )
        notification_channels = ask_default(
            "Notification channels",
            current.get("ATTUNE_NOTIFICATION_CHANNELS", route_default),
        )
        interaction_channels = ask_default(
            "Interaction channels",
            current.get("ATTUNE_INTERACTION_CHANNELS", route_default),
        )

        visibility_ack = current.get("ATTUNE_ACK_DESTINATION_VISIBILITY", "")
        if (slack_channel and not slack_channel.startswith("D")) or chat_space:
            visibility_ack = ask_default(
                "Destination membership verified (yes/no)", visibility_ack or "no"
            )
        timezone = ask_default(
            "Timezone (IANA name)", current.get("ATTUNE_TIMEZONE", "UTC")
        )
        brief_time = ask_default(
            "Morning brief time", current.get("ATTUNE_BRIEF_TIME", "07:30")
        )

    updates = {
        "ATTUNE_WORKSPACE_BACKEND": backend,
        "ATTUNE_INGESTION_MODE": ingestion,
        "ATTUNE_USER_ID": user_id,
        "ATTUNE_INTERNAL_DOMAINS": internal_domains,
        "ATTUNE_DATA_DIR": data_dir,
        "ATTUNE_LLM_BASE_URL": llm_base,
        "ATTUNE_LLM_API_KEY": llm_key,
        "ATTUNE_MODEL_DEFAULT": model_default,
        **models,
        "ATTUNE_EMBEDDING_BASE_URL": embedding_base,
        "ATTUNE_EMBEDDING_API_KEY": embedding_key,
        "ATTUNE_EMBEDDING_MODEL": embedding_model,
        "ATTUNE_EMBEDDING_DIMENSIONS": embedding_dims,
        "GOOGLE_PROJECT_ID": google_project,
        "ATTUNE_GOOGLE_CREDENTIALS_FILE": google_creds,
        "ATTUNE_MCP_URL": mcp_url,
        "ATTUNE_MCP_GMAIL_URL": mcp_gmail_url,
        "ATTUNE_MCP_CALENDAR_URL": mcp_calendar_url,
        "ATTUNE_MCP_TOKEN": mcp_token,
        "SLACK_BOT_TOKEN": slack_bot,
        "SLACK_APP_TOKEN": slack_app,
        "ATTUNE_SLACK_CHANNEL": slack_channel,
        "ATTUNE_SLACK_ALLOWED_USERS": slack_allowed,
        "ATTUNE_CHAT_SPACE": chat_space,
        "ATTUNE_CHAT_CREDENTIALS_FILE": chat_creds,
        "ATTUNE_CHAT_ALLOWED_USERS": chat_allowed,
        "ATTUNE_BRIEF_CHANNELS": brief_channels,
        "ATTUNE_APPROVAL_CHANNEL": approval_channel,
        "ATTUNE_NOTIFICATION_CHANNELS": notification_channels,
        "ATTUNE_INTERACTION_CHANNELS": interaction_channels,
        "ATTUNE_ACK_DESTINATION_VISIBILITY": (
            "1" if visibility_ack.lower() in {"1", "true", "yes", "y"} else ""
        ),
        "ATTUNE_TIMEZONE": timezone,
        "ATTUNE_BRIEF_TIME": brief_time,
    }
    content = _rewrite_env(original, updates)
    _atomic_write(env_file, content, backup=existed and not (fresh or force))
    backup_note = (
        f"; backup: {env_file}.bak" if existed and not (fresh or force) else ""
    )
    out(f"Wrote {env_file} (0600){backup_note}")
    if quick:
        out(
            "Quick setup skipped: ingestion mode, per-task model overrides, "
            "Google/MCP workspace credentials, Slack/Google Chat channels, "
            "and timezone/brief time (each kept its current value or a safe "
            "default)."
        )
        out(
            "Follow-up: `attune init --google-setup` for the guided Google "
            "Cloud checklist; `attune init` (without --quick) for the full "
            "wizard to add channels."
        )
    reminder = _external_testing_reminder(data_dir)
    if reminder:
        out(reminder)
    if target == "configure":
        out("Next: attune doctor, then attune brief")
        return 0
    return _run_local_target(
        env_file=env_file,
        data_dir=data_dir,
        content=content,
        ask=ask,
        yes=yes,
        runner=local_runner,
        doctor=doctor,
        force_apply=False,
        out=out,
    )


def _run_local_target(
    *,
    env_file: str,
    data_dir: str,
    content: str,
    ask: Callable[[str], str],
    yes: bool,
    runner: (
        Callable[
            [list[str] | tuple[str, ...]], subprocess.CompletedProcess[str]
        ]
        | None
    ),
    doctor: Callable[..., int] | None,
    force_apply: bool = False,
    out: Callable[[str], None],
) -> int:
    """Apply and validate the deterministic local substrate plan.

    The state document contains only workflow metadata and a one-way digest of
    the environment file, never its values.  An interrupted ``in_progress``
    apply is safe to retry because Docker Compose ``up`` is idempotent.
    """
    from .env_file import attune_env_exact
    from .local_setup import (
        LocalProvisionError,
        apply_local_plan,
        build_local_plan,
        render_local_plan,
    )
    from .setup_state import SetupState, SetupStateError, setup_state_path

    path = setup_state_path(data_dir)
    try:
        state = SetupState.load_or_create(
            path,
            target="local",
            env_file=env_file,
            data_dir=data_dir,
        )
    except SetupStateError as exc:
        out(f"Setup state refused: {exc}")
        out(f"Inspect or move {path}; Attune will not overwrite ambiguous setup state.")
        return 1

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    state.record_configuration(digest)
    plan = build_local_plan()
    state.record_plan(plan.digest)
    state.save(path)
    for line in render_local_plan(plan):
        out(line)
    approved = yes or (
        ask("Apply this local deployment plan? (y/N): ").strip().lower() == "y"
    )
    if not approved:
        state.set_step("apply", "declined", "operator declined the displayed plan")
        state.save(path)
        out(
            "Configuration is saved. Rerun with --target local when ready; "
            f"state: {path}"
        )
        return 0

    if state.steps["apply"].status == "succeeded" and not force_apply:
        out(
            "Local resources were already applied for this configuration; "
            "validating again."
        )
    else:
        state.set_step("apply", "in_progress", "applying reviewed Docker Compose plan")
        state.save(path)
        try:
            apply_local_plan(plan, runner=runner)
        except LocalProvisionError as exc:
            state.set_step("apply", "failed", "Docker Compose plan failed")
            state.save(path)
            out(f"Local deployment failed: {exc}")
            out(f"Fix Docker and rerun the same command; resumable state: {path}")
            return 1
        state.resources = list(plan.resources)
        state.set_step("apply", "succeeded", "reviewed Docker Compose plan applied")
        state.save(path)
        out("Local Qdrant deployment applied.")

    state.set_step("validate", "in_progress", "running full Attune Doctor")
    state.save(path)
    if doctor is None:
        # The CLI loaded .env before the wizard rewrote it. Refresh only from
        # the explicit setup file so Doctor validates the values just written.
        from .doctor import run_doctor

        doctor = run_doctor
        with attune_env_exact(env_file):
            code = int(doctor(out=out) or 0)
    else:
        code = int(doctor(out=out) or 0)
    if code:
        state.set_step("validate", "failed", "Attune Doctor reported failures")
        state.save(path)
        out(
            "Local deployment is running but validation failed; resumable "
            f"state: {path}"
        )
        return code
    state.set_step("validate", "succeeded", "Attune Doctor passed")
    state.save(path)
    out(f"Local setup completed and validated; state: {path}")
    out("Next: attune brief, then attune run")
    return 0


def _google_credentials_step(
    *,
    ask,
    ask_default,
    out,
    data_dir: str,
    oauth_flow,
    default: str = "",
    google_setup: Callable[..., object] | None = None,
) -> str:
    path = ask_default(
        "Google credentials JSON (authorized user, service account, or OAuth client)",
        default,
    )
    resolved = os.path.expanduser(path) if path else ""
    if not resolved or not os.path.exists(resolved):
        offer = ask(
            "No Google OAuth client file found yet. Walk through the guided "
            "Google Cloud setup checklist now? (y/N): "
        ).strip().lower()
        if offer == "y":
            from .google_setup_cmd import run_google_setup as _default_google_setup

            setup_fn = google_setup or _default_google_setup
            setup_fn(data_dir=data_dir, ask=ask, out=out)
            path = ask_default(
                "Google credentials JSON (authorized user, service account, "
                "or OAuth client)",
                path,
            )
    if not path:
        return ""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        out(f"  note: {path} does not exist yet; preserving the setting")
        return path
    try:
        with open(path) as fh:
            data = json.load(fh)
    except ValueError:
        return path
    if "installed" not in data and "web" not in data:
        return path
    if ask("Run Google consent flow now? (y/N): ").strip().lower() != "y":
        return path
    flow = oauth_flow or _run_oauth_flow
    saved = flow(client_secret_path=path, save_dir=data_dir)
    out(f"  Authorized-user credentials saved to {saved}")
    return saved


def _run_oauth_flow(*, client_secret_path: str, save_dir: str) -> str:  # pragma: no cover
    from google_auth_oauthlib.flow import InstalledAppFlow

    from ..credentials import SCOPES_DEFAULT

    flow = InstalledAppFlow.from_client_secrets_file(
        client_secret_path, scopes=list(SCOPES_DEFAULT)
    )
    creds = flow.run_local_server(port=0)
    save_path = os.path.join(save_dir, "google_authorized_user.json")
    with open(save_path, "w") as fh:
        fh.write(creds.to_json())
    os.chmod(save_path, 0o600)
    return save_path
