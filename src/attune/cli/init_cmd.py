"""``attune init``: create, edit, and migrate an Attune environment file."""

from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import tempfile
from typing import Callable

from dotenv import dotenv_values

DEFAULT_DATA_DIR = "~/.attune"

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
    out: Callable[[str], None] = print,
) -> int:
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

    backend = ask_default(
        "Workspace backend (google_oauth/mcp)",
        current.get("ATTUNE_WORKSPACE_BACKEND", "google_oauth"),
    )
    ingestion = ask_default(
        "Ingestion mode (poll/google_pubsub)",
        current.get("ATTUNE_INGESTION_MODE", "poll"),
    )
    data_dir = os.path.expanduser(
        ask_default("Data directory", current.get("ATTUNE_DATA_DIR", DEFAULT_DATA_DIR))
    )
    if data_dir:
        os.makedirs(data_dir, exist_ok=True)
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
        "Default chat model", current.get("ATTUNE_MODEL_DEFAULT", "")
    )
    model_prompts = {
        "ATTUNE_MODEL_CLASSIFY": "Classification model",
        "ATTUNE_MODEL_DRAFT": "Drafting model",
        "ATTUNE_MODEL_REASON": "Reasoning model",
        "ATTUNE_MODEL_CONSOLIDATE": "Consolidation model",
        "ATTUNE_MODEL_CONVERSE": "Conversation model",
        "ATTUNE_MODEL_MEMORY_EXTRACT": "Memory extraction model",
    }
    models = {
        key: ask_default(prompt, current.get(key, model_default))
        for key, prompt in model_prompts.items()
    }
    embedding_base = ask_default(
        "Embedding API base URL",
        current.get("ATTUNE_EMBEDDING_BASE_URL", llm_base),
    )
    embedding_key = ask_kept_secret(
        "Embedding API key (blank uses LLM key)", "ATTUNE_EMBEDDING_API_KEY"
    )
    embedding_model = ask_default(
        "Embedding model", current.get("ATTUNE_EMBEDDING_MODEL", "")
    )
    embedding_dims = ask_default(
        "Embedding dimensions", current.get("ATTUNE_EMBEDDING_DIMENSIONS", "")
    )

    google_project = current.get("GOOGLE_PROJECT_ID", "")
    google_creds = current.get("ATTUNE_GOOGLE_CREDENTIALS_FILE", "")
    mcp_url = current.get("ATTUNE_MCP_URL", "")
    mcp_gmail_url = current.get("ATTUNE_MCP_GMAIL_URL", "")
    mcp_calendar_url = current.get("ATTUNE_MCP_CALENDAR_URL", "")
    mcp_token = current.get("ATTUNE_MCP_TOKEN", "")
    if backend == "google_oauth":
        google_project = ask_default("Google Cloud project ID", google_project)
        google_creds = _google_credentials_step(
            ask=ask,
            ask_default=ask_default,
            out=out,
            data_dir=data_dir,
            oauth_flow=oauth_flow,
            default=google_creds,
        )
    else:
        mcp_url = ask_default("MCP Streamable HTTP URL", mcp_url)
        if not mcp_url:
            mcp_gmail_url = ask_default("Gmail MCP URL", mcp_gmail_url)
            mcp_calendar_url = ask_default("Calendar MCP URL", mcp_calendar_url)
        mcp_token = ask_kept_secret("MCP bearer token", "ATTUNE_MCP_TOKEN")

    slack_bot = ask_kept_secret("Slack bot token", "SLACK_BOT_TOKEN")
    slack_app = ask_kept_secret("Slack app-level token", "SLACK_APP_TOKEN") if slack_bot else ""
    slack_channel = ask_default(
        "Slack proactive destination ID", current.get("ATTUNE_SLACK_CHANNEL", "")
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

    available = [name for name, ok in (("slack", slack_bot), ("google_chat", chat_space)) if ok]
    route_default = ",".join(available)
    brief_channels = ask_default(
        "Brief channels", current.get("ATTUNE_BRIEF_CHANNELS", route_default)
    )
    approval_channel = ask_default(
        "Approval channel", current.get("ATTUNE_APPROVAL_CHANNEL", available[0] if available else "")
    )
    notification_channels = ask_default(
        "Notification channels", current.get("ATTUNE_NOTIFICATION_CHANNELS", route_default)
    )
    interaction_channels = ask_default(
        "Interaction channels", current.get("ATTUNE_INTERACTION_CHANNELS", route_default)
    )

    visibility_ack = current.get("ATTUNE_ACK_DESTINATION_VISIBILITY", "")
    if (slack_channel and not slack_channel.startswith("D")) or chat_space:
        visibility_ack = ask_default(
            "Destination membership verified (yes/no)", visibility_ack or "no"
        )
    timezone = ask_default("Timezone (IANA name)", current.get("ATTUNE_TIMEZONE", "UTC"))
    brief_time = ask_default("Morning brief time", current.get("ATTUNE_BRIEF_TIME", "07:30"))

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
    out(f"Wrote {env_file} (0600)" + (f"; backup: {env_file}.bak" if existed and not (fresh or force) else ""))
    out("Next: attune doctor, then attune brief")
    return 0


def _google_credentials_step(
    *, ask, ask_default, out, data_dir: str, oauth_flow, default: str = ""
) -> str:
    path = ask_default(
        "Google credentials JSON (authorized user, service account, or OAuth client)",
        default,
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

    flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, scopes=list(SCOPES_DEFAULT))
    creds = flow.run_local_server(port=0)
    save_path = os.path.join(save_dir, "google_authorized_user.json")
    with open(save_path, "w") as fh:
        fh.write(creds.to_json())
    os.chmod(save_path, 0o600)
    return save_path
