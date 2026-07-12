"""``aidedecamp init`` — the interactive setup wizard (roadmap prompt 08).

Asks the handful of questions that matter, writes a grouped, commented
``.env``, and (optionally) runs the Google OAuth consent flow so the user
never hand-assembles an authorized-user file. Secrets go to the env file
only — never echoed, never logged (rule 6) — and the file is chmod 0600.

The OAuth consent flow (``InstalledAppFlow.run_local_server``) is the one
documented exception to the no-inbound-port rule: a short-lived localhost
redirect listener during interactive setup, user-initiated, gone when the
consent completes — not a service port on the running process. Scopes come
from ``credentials.SCOPES_DEFAULT``; ``gmail.send`` is never requested
(rule 4 — enabling send is a separately-reviewed change, not a setup step).

Every prompt/secret/flow collaborator is injectable so tests script the
whole wizard offline.
"""

from __future__ import annotations

import getpass
import json
import os
from typing import Any, Callable

DEFAULT_DATA_DIR = "~/.aidedecamp"


def run_init(
    *,
    env_file: str = ".env",
    force: bool = False,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
    oauth_flow: Callable[..., str] | None = None,
    out: Callable[[str], None] = print,
) -> int:
    """Run the wizard and write ``env_file``. Returns a process exit code."""
    if os.path.exists(env_file) and not force:
        out(f"{env_file} already exists — re-run with --force to overwrite.")
        return 1

    def ask_default(prompt: str, default: str) -> str:
        answer = ask(f"{prompt} [{default}]: ").strip()
        return answer or default

    out("aidedecamp setup — answers are written to " + env_file)

    deployment = ask_default("Deployment (personal/telus)", "personal")
    connector = ask_default(
        "Connector mode (direct_oauth/mcp — direct_oauth works with plain "
        "OAuth credentials today)",
        "direct_oauth",
    )
    ingestion = ask_default("Ingestion mode (poll/push)", "poll")

    data_dir = os.path.expanduser(
        ask_default("Data directory (state, memory, audit log)", DEFAULT_DATA_DIR)
    )
    os.makedirs(data_dir, exist_ok=True)

    user_id = ask_default(
        "Google mailbox email (also the single memory principal)", "me"
    )
    google_project = ask_default("Google Cloud project ID", "")

    fuelix_token = ask_secret("Fuel iX bearer token (hidden, blank to skip): ")

    google_creds = _google_credentials_step(
        ask=ask, ask_default=ask_default, out=out,
        data_dir=data_dir, oauth_flow=oauth_flow,
    )

    slack_bot = ask_secret("Slack bot token (hidden, blank to skip): ")
    slack_app = (
        ask_secret("Slack app-level token (hidden, blank to skip): ")
        if slack_bot
        else ""
    )
    slack_channel = (
        ask_default("Slack channel for proactive posts (e.g. #aide)", "")
        if slack_bot
        else ""
    )
    slack_allowed = (
        ask_default(
            "Your Slack user ID (allowlist — only these IDs may command the "
            "assistant; comma-separated)",
            "",
        )
        if slack_bot
        else ""
    )
    chat_space = ask_default("Google Chat space (spaces/..., blank to skip)", "")
    chat_allowed = (
        ask_default(
            "Your Chat user ID (users/..., allowlist; comma-separated)", ""
        )
        if chat_space
        else ""
    )
    visibility_ack = ""
    if (slack_channel and not slack_channel.startswith("D")) or chat_space:
        visibility_ack = ask_default(
            "I verified every proactive channel/space is owner-only, or accept "
            "that its members can read briefs and drafts (yes/no)",
            "no",
        )

    tz = ask_default("Timezone (IANA name)", "UTC")
    brief_time = ask_default("Morning brief time (HH:MM, local)", "07:30")

    lines = [
        "# aidedecamp settings — written by `aidedecamp init`",
        "# Secrets live here and only here; this file is gitignored.",
        "",
        "# --- deployment ---",
        f"ADC_DEPLOYMENT={deployment}",
        f"ADC_CONNECTOR_MODE={connector}",
        f"ADC_INGESTION_MODE={ingestion}",
        f"ADC_USER_ID={user_id}",
        f"ADC_DATA_DIR={data_dir}",
        "",
        "# --- model gateway ---",
    ]
    if fuelix_token:
        lines.append(f"FUELIX_TOKEN={fuelix_token}")
    else:
        lines.append("# FUELIX_TOKEN=  (required before anything hits Fuel iX)")
    lines += ["", "# --- google ---"]
    if google_project:
        lines.append(f"GOOGLE_PROJECT_ID={google_project}")
    if google_creds:
        lines.append(f"ADC_GOOGLE_CREDENTIALS_FILE={google_creds}")
    else:
        lines.append(
            "# ADC_GOOGLE_CREDENTIALS_FILE=  (absent -> Application Default "
            "Credentials)"
        )
    lines += ["", "# --- channels ---"]
    if slack_bot:
        lines.append(f"SLACK_BOT_TOKEN={slack_bot}")
        if slack_app:
            lines.append(f"SLACK_APP_TOKEN={slack_app}")
        if slack_channel:
            lines.append(f"ADC_SLACK_CHANNEL={slack_channel}")
        if slack_allowed:
            lines.append(f"ADC_SLACK_ALLOWED_USERS={slack_allowed}")
    else:
        lines.append("# SLACK_BOT_TOKEN= / SLACK_APP_TOKEN= / ADC_SLACK_CHANNEL=")
    if chat_space:
        lines.append(f"ADC_CHAT_SPACE={chat_space}")
        if chat_allowed:
            lines.append(f"ADC_CHAT_ALLOWED_USERS={chat_allowed}")
    else:
        lines.append("# ADC_CHAT_SPACE=")
    if visibility_ack.strip().lower() in {"y", "yes", "1", "true"}:
        lines.append("ADC_ACK_DESTINATION_VISIBILITY=1")
    lines += [
        "",
        "# --- cadence ---",
        f"ADC_TIMEZONE={tz}",
        f"ADC_BRIEF_TIME={brief_time}",
        "",
    ]

    with open(env_file, "w") as fh:
        fh.write("\n".join(lines))
    os.chmod(env_file, 0o600)

    out("")
    out(f"Wrote {env_file} (0600). Next steps:")
    out("  1. aidedecamp doctor   — validate everything")
    out("  2. aidedecamp brief    — your first brief, in the terminal")
    if ingestion == "poll":
        out("  3. aidedecamp run      — the always-on process (polling mode)")
    else:
        out(
            "  3. push mode needs the GCP Pub/Sub + republisher setup — see "
            "docs/deployment.md"
        )
    return 0


def _google_credentials_step(
    *,
    ask: Callable[[str], str],
    ask_default: Callable[[str, str], str],
    out: Callable[[str], None],
    data_dir: str,
    oauth_flow: Callable[..., str] | None,
) -> str:
    """Resolve a Google credentials file: use one as-is, or run the OAuth
    consent flow when the user points at an OAuth *client secret* file."""
    path = ask_default(
        "Google credentials JSON (service account, authorized user, or an "
        "OAuth client secret; blank for ADC)",
        "",
    )
    if not path:
        return ""
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        out(f"  note: {path} does not exist yet — writing the setting anyway.")
        return path

    with open(path) as fh:
        try:
            data = json.load(fh)
        except ValueError:
            out("  note: not valid JSON — writing the setting anyway.")
            return path

    if "installed" not in data and "web" not in data:
        return path  # service-account or authorized-user file: usable as-is

    # An OAuth client secret: offer to run the consent flow now.
    answer = ask(
        "  That's an OAuth client secret. Run the Google consent flow now to "
        "create an authorized-user file? (y/N): "
    ).strip().lower()
    if answer != "y":
        return path

    flow = oauth_flow or _run_oauth_flow
    saved = flow(client_secret_path=path, save_dir=data_dir)
    out(f"  Authorized-user credentials saved to {saved}")
    return saved


def _run_oauth_flow(
    *, client_secret_path: str, save_dir: str
) -> str:  # pragma: no cover - opens a browser + localhost listener
    """The real consent flow (google-auth-oauthlib, `[google]` extra).

    Scopes are SCOPES_DEFAULT — read+compose for Gmail and Calendar event
    access. Optional Chat authorization is deferred until its separate app-auth
    credential is production-wired. Never request gmail.send (rule 4).
    """
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
