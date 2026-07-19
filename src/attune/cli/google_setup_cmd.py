"""``attune init --google-setup``: a guided, resumable checklist for the
Google Cloud Console ceremony that ``attune init`` cannot see or perform on
its own (docs/getting-started.md section 4A; UX review persona A item #1,
G20).

Every step only ever prints copy-paste values (project URL, consent-screen
URL, exact OAuth scope strings pulled live from ``credentials.py`` so they
can never drift from code) and waits for an explicit confirm/skip answer.
The two ``gcloud services enable`` steps are the only ones Attune can run
for the operator, and only with a fixed argument list, no shell, and no
Attune environment passed through — the same discipline
``local_setup.py``'s Docker Compose runner uses — after the operator
explicitly confirms AND ``gcloud`` is on PATH. Nothing here ever creates a
cloud resource silently. Progress and the final Internal/External+Testing
answer are recorded in secret-free state (``google_setup_state.py``), never
in ``.env``.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from typing import Callable, Sequence

from ..credentials import SCOPES_DEFAULT
from .google_setup_state import GoogleSetupState, STEP_IDS, google_setup_state_path
from .local_setup import scrubbed_subprocess_env
from .setup_state import SetupStateError

GcloudRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

PROJECT_CREATE_URL = "https://console.cloud.google.com/projectcreate"
AUTH_PLATFORM_BRANDING_URL = "https://console.cloud.google.com/auth/branding"
AUTH_PLATFORM_AUDIENCE_URL = "https://console.cloud.google.com/auth/audience"
AUTH_PLATFORM_SCOPES_URL = "https://console.cloud.google.com/auth/scopes"
AUTH_PLATFORM_CLIENTS_URL = "https://console.cloud.google.com/auth/clients"

GMAIL_ENABLE_COMMAND: tuple[str, ...] = ("gcloud", "services", "enable", "gmail.googleapis.com")
CALENDAR_ENABLE_COMMAND: tuple[str, ...] = (
    "gcloud", "services", "enable", "calendar-json.googleapis.com",
)


def _default_gcloud_runner(command: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(command), check=False, capture_output=True, text=True, shell=False,
        env=scrubbed_subprocess_env(),
    )


def _step_titles(data_dir: str) -> dict[str, tuple[str, list[str]]]:
    client_secret_path = os.path.join(data_dir, "google_client_secret.json")
    return {
        "create_project": (
            "Create or select a Google Cloud project",
            [
                f"Open {PROJECT_CREATE_URL}",
                "Record the Project ID shown (not the display name) — you "
                "paste this into `attune init` as the Google Cloud project ID.",
            ],
        ),
        "enable_gmail_api": (
            "Enable the Gmail API",
            [f"Command: {shlex.join(GMAIL_ENABLE_COMMAND)}"],
        ),
        "enable_calendar_api": (
            "Enable the Google Calendar API",
            [f"Command: {shlex.join(CALENDAR_ENABLE_COMMAND)}"],
        ),
        "consent_branding": (
            "Configure the OAuth consent screen branding",
            [
                f"Open {AUTH_PLATFORM_BRANDING_URL}",
                "Set the app name to `Attune`, add a support email and a "
                "developer contact email, then save.",
            ],
        ),
        "consent_scopes": (
            "Add the exact OAuth scopes",
            [
                f"Open {AUTH_PLATFORM_SCOPES_URL}",
                "Add exactly these scopes (copy-paste, one per line):",
                *[f"  {scope}" for scope in SCOPES_DEFAULT],
            ],
        ),
        "oauth_client": (
            "Create a Desktop OAuth client",
            [
                f"Open {AUTH_PLATFORM_CLIENTS_URL}",
                "Create Client -> Desktop app -> Create, then download its JSON.",
                f"Save the downloaded file as {client_secret_path}",
                "(or any path — you will paste it into `attune init`).",
            ],
        ),
    }


def _confirm_step(
    step_id: str,
    title: str,
    lines: list[str],
    *,
    state: GoogleSetupState,
    ask: Callable[[str], str],
    out: Callable[[str], None],
    step_number: int,
    total_steps: int,
    runnable_command: Sequence[str] | None = None,
    gcloud_runner: GcloudRunner | None = None,
    gcloud_available: bool = False,
) -> None:
    existing = state.steps[step_id]
    out(f"\n[{step_number}/{total_steps}] {title}")
    if existing.status in {"succeeded", "skipped"}:
        suffix = f" — {existing.detail}" if existing.detail else ""
        out(f"  already {existing.status}{suffix}")
        return
    for line in lines:
        out(f"  {line}")
    if runnable_command and gcloud_available:
        run_answer = ask("  Run this command now with gcloud? (y/N): ").strip().lower()
        if run_answer == "y":
            runner = gcloud_runner or _default_gcloud_runner
            result = runner(runnable_command)
            if result.returncode == 0:
                state.set_step(step_id, "succeeded", "ran via gcloud")
                out("  gcloud command succeeded.")
                return
            detail = (result.stderr or result.stdout or "gcloud command failed").strip()
            state.set_step(step_id, "failed", detail)
            out(f"  gcloud command failed: {detail}")
            out("  Fix the issue and rerun `attune init --google-setup` to retry.")
            return
    answer = ask(
        "  Mark as done? (y = confirm, s = skip, Enter = pause here): "
    ).strip().lower()
    if answer == "y":
        state.set_step(step_id, "succeeded", "confirmed by operator")
    elif answer == "s":
        state.set_step(step_id, "skipped", "operator skipped")
    else:
        out("  Paused. Rerun `attune init --google-setup` to resume from this step.")


def _consent_audience_step(
    *,
    state: GoogleSetupState,
    ask: Callable[[str], str],
    out: Callable[[str], None],
    step_number: int,
    total_steps: int,
) -> None:
    step_id = "consent_audience"
    existing = state.steps[step_id]
    out(f"\n[{step_number}/{total_steps}] Choose Internal or External+Testing")
    if existing.status in {"succeeded", "skipped"}:
        note = f" ({state.consent_mode})" if state.consent_mode else ""
        out(f"  already {existing.status}{note}")
        return
    out(f"  Open {AUTH_PLATFORM_AUDIENCE_URL}")
    out("  Internal: Workspace-owned projects restricted to your organization.")
    out(
        "  External + Testing: personal Google accounts; add yourself under "
        "Test users."
    )
    answer = ask(
        "  Which did you choose? (internal/external, Enter to pause): "
    ).strip().lower()
    if answer.startswith("i"):
        state.set_consent_mode("internal")
        state.set_step(step_id, "succeeded", "internal")
    elif answer.startswith("e"):
        state.set_consent_mode("external_testing")
        state.set_step(step_id, "succeeded", "external_testing")
    else:
        out("  Paused. Rerun `attune init --google-setup` to resume from this step.")


def run_google_setup(
    *,
    data_dir: str,
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    gcloud_runner: GcloudRunner | None = None,
    gcloud_path: Callable[[], str | None] = lambda: shutil.which("gcloud"),
) -> GoogleSetupState:
    """Walk the resumable Google Cloud checklist; return the recorded state."""
    path = google_setup_state_path(data_dir)
    try:
        state = GoogleSetupState.load_or_create(path)
    except SetupStateError as exc:
        out(f"Google setup checklist refused: {exc}")
        out(f"Inspect or move {path}; Attune will not overwrite ambiguous state.")
        raise

    out(f"Guided Google Cloud setup checklist (resumable; state: {path})")
    gcloud_available = gcloud_path() is not None
    if not gcloud_available:
        out(
            "Note: `gcloud` is not on PATH; the two API-enable steps show "
            "copy-paste commands only."
        )

    titles = _step_titles(data_dir)
    runnable = {
        "enable_gmail_api": GMAIL_ENABLE_COMMAND,
        "enable_calendar_api": CALENDAR_ENABLE_COMMAND,
    }
    total = len(STEP_IDS)
    for index, step_id in enumerate(STEP_IDS, start=1):
        if step_id == "consent_audience":
            _consent_audience_step(
                state=state, ask=ask, out=out, step_number=index, total_steps=total
            )
        else:
            title, lines = titles[step_id]
            _confirm_step(
                step_id, title, lines,
                state=state, ask=ask, out=out,
                step_number=index, total_steps=total,
                runnable_command=runnable.get(step_id),
                gcloud_runner=gcloud_runner,
                gcloud_available=gcloud_available,
            )
        state.save(path)

    if state.consent_mode == "external_testing":
        out(
            "\nReminder: External+Testing refresh tokens expire ~7 days "
            "after issuance. `attune doctor` will flag this (check: "
            "google-oauth-app) until you switch to Internal or publish the app."
        )
    out(
        "\nNext: run `attune init` and point the Google credentials JSON "
        "question at the downloaded Desktop client file."
    )
    return state


def run_google_setup_command(
    *,
    env_file: str = ".env",
    ask: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    gcloud_runner: GcloudRunner | None = None,
) -> int:
    """Standalone ``attune init --google-setup`` entry point."""
    from dotenv import dotenv_values

    from .init_cmd import DEFAULT_DATA_DIR

    raw = DEFAULT_DATA_DIR
    if os.path.exists(env_file):
        raw = dotenv_values(env_file).get("ATTUNE_DATA_DIR") or DEFAULT_DATA_DIR
    data_dir = os.path.expanduser(raw)
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except OSError:
        pass
    try:
        run_google_setup(data_dir=data_dir, ask=ask, out=out, gcloud_runner=gcloud_runner)
    except SetupStateError:
        return 1
    return 0
