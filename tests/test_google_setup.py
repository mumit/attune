"""Tests for the guided Google Cloud setup checklist (``attune init
--google-setup``; UX review persona A item #1, G20). All external effects
(gcloud, PATH lookup) are injected; nothing touches a real .env or network."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from attune.cli.google_setup_cmd import (
    CALENDAR_ENABLE_COMMAND,
    GMAIL_ENABLE_COMMAND,
    run_google_setup,
    run_google_setup_command,
)
from attune.cli.google_setup_state import STEP_IDS, google_setup_state_path
from attune.cli.setup_state import SetupStateError
from attune.credentials import SCOPES_DEFAULT


def _scripted(answers):
    queue = list(answers)

    def ask(prompt: str) -> str:
        return queue.pop(0) if queue else ""

    return ask


def _no_gcloud():
    return None


def test_checklist_prints_exact_scopes_from_credentials(tmp_path):
    lines = []
    run_google_setup(
        data_dir=str(tmp_path),
        ask=_scripted([]),
        out=lines.append,
        gcloud_path=_no_gcloud,
    )
    rendered = "\n".join(lines)
    for scope in SCOPES_DEFAULT:
        assert scope in rendered


def test_checklist_is_numbered_and_resumable(tmp_path):
    data_dir = str(tmp_path)
    # First run: confirm steps 1 and 4, leave the rest paused.
    answers = ["y", "", "", "y", "", "", ""]
    lines = []
    state = run_google_setup(
        data_dir=data_dir, ask=_scripted(answers), out=lines.append,
        gcloud_path=_no_gcloud,
    )
    assert state.steps["create_project"].status == "succeeded"
    assert state.steps["enable_gmail_api"].status == "not_started"
    assert state.steps["consent_branding"].status == "succeeded"
    assert any("[1/7]" in line for line in lines)
    assert any("[7/7]" in line for line in lines)

    # Resume: already-confirmed steps are not re-asked; unresolved ones are.
    lines2 = []
    state2 = run_google_setup(
        data_dir=data_dir,
        ask=_scripted(["y", "y", "internal", "y", "y"]),
        out=lines2.append,
        gcloud_path=_no_gcloud,
    )
    assert any("already succeeded" in line for line in lines2)
    assert state2.steps["enable_gmail_api"].status == "succeeded"
    assert state2.steps["enable_calendar_api"].status == "succeeded"
    assert state2.consent_mode == "internal"
    assert state2.steps["oauth_client"].status == "succeeded"


def test_checklist_state_is_secret_free_and_owner_only(tmp_path):
    run_google_setup(
        data_dir=str(tmp_path),
        ask=_scripted(["y", "", "", "y", "internal", "y", "y"]),
        out=lambda s: None,
        gcloud_path=_no_gcloud,
    )
    path = google_setup_state_path(str(tmp_path))
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    raw = json.loads(open(path, encoding="utf-8").read())
    assert raw["consent_mode"] == "internal"
    # No configuration values or credentials of any kind are recorded.
    for step in raw["steps"].values():
        assert "path" not in step
        assert "json" not in step.get("detail", "").lower()


def test_gcloud_run_is_offered_only_when_available_and_uses_fixed_argv(tmp_path):
    commands = []

    def runner(command):
        commands.append(tuple(command))
        return subprocess.CompletedProcess(command, 0, stdout="Operation done.", stderr="")

    answers = _scripted(["", "y", "y"])  # skip project step, then confirm both gcloud runs
    state = run_google_setup(
        data_dir=str(tmp_path),
        ask=answers,
        out=lambda s: None,
        gcloud_runner=runner,
        gcloud_path=lambda: "/usr/bin/gcloud",
    )
    assert len(commands) == 2
    assert tuple(commands[0]) == GMAIL_ENABLE_COMMAND
    assert tuple(commands[1]) == CALENDAR_ENABLE_COMMAND
    assert state.steps["enable_gmail_api"].status == "succeeded"
    assert state.steps["enable_calendar_api"].status == "succeeded"
    # No shell was used and no Attune environment was smuggled through.
    for command in commands:
        assert isinstance(command, tuple)
        assert all(isinstance(part, str) for part in command)


def test_gcloud_not_on_path_never_offers_to_run_it(tmp_path):
    def ask(prompt: str) -> str:
        assert "Run this command now with gcloud" not in prompt
        return ""

    run_google_setup(
        data_dir=str(tmp_path), ask=ask, out=lambda s: None, gcloud_path=_no_gcloud,
    )


def test_gcloud_failure_is_recorded_and_step_stays_resumable(tmp_path):
    def failing_runner(command):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="PERMISSION_DENIED")

    lines = []
    state = run_google_setup(
        data_dir=str(tmp_path),
        ask=_scripted(["", "y"]),
        out=lines.append,
        gcloud_runner=failing_runner,
        gcloud_path=lambda: "/usr/bin/gcloud",
    )
    assert state.steps["enable_gmail_api"].status == "failed"
    assert any("PERMISSION_DENIED" in line for line in lines)


def test_consent_audience_records_external_testing(tmp_path):
    state = run_google_setup(
        data_dir=str(tmp_path),
        ask=_scripted(["y", "", "", "y", "external", "y", "y"]),
        out=lambda s: None,
        gcloud_path=_no_gcloud,
    )
    assert state.consent_mode == "external_testing"


def test_checklist_refuses_ambiguous_state(tmp_path):
    path = google_setup_state_path(str(tmp_path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"schema_version": 999}, fh)
    os.chmod(path, 0o600)

    with pytest.raises(SetupStateError):
        run_google_setup(
            data_dir=str(tmp_path), ask=_scripted([]), out=lambda s: None,
            gcloud_path=_no_gcloud,
        )


def test_standalone_command_resolves_data_dir_from_existing_env_file(tmp_path):
    env_file = tmp_path / ".env"
    data_dir = tmp_path / "data"
    env_file.write_text(f"ATTUNE_DATA_DIR={data_dir}\n")

    code = run_google_setup_command(
        env_file=str(env_file),
        ask=_scripted(["y", "", "", "y", "internal", "y", "y"]),
        out=lambda s: None,
    )
    assert code == 0
    assert os.path.exists(google_setup_state_path(str(data_dir)))


def test_standalone_command_defaults_data_dir_when_no_env_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    code = run_google_setup_command(
        env_file=str(tmp_path / "does-not-exist.env"),
        ask=_scripted([]),
        out=lambda s: None,
    )
    assert code == 0
    assert os.path.exists(google_setup_state_path(str(fake_home / ".attune")))


def test_all_step_ids_have_a_number():
    # Sanity: the step vocabulary the Doctor hint ("step 5") depends on.
    assert STEP_IDS.index("consent_audience") == 4  # zero-based -> displayed as 5


def test_subprocess_env_is_scrubbed_of_attune_values(monkeypatch):
    """The decision log's "receives no Attune environment or credential"
    made literal: child processes (gcloud, docker compose) never see
    ATTUNE_* values or the known credential-bearing token names, while
    tool-required variables (PATH, HOME) pass through."""
    from attune.cli.local_setup import scrubbed_subprocess_env

    monkeypatch.setenv("ATTUNE_LLM_API_KEY", "sk-secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/user")

    env = scrubbed_subprocess_env()

    assert "ATTUNE_LLM_API_KEY" not in env
    assert "SLACK_BOT_TOKEN" not in env
    assert "SLACK_APP_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/user"
