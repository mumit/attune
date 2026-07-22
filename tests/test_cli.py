"""Tests for the CLI (roadmap prompt 08) — wizard, doctor, brief, parser.
Everything offline: prompts/secrets/flows/checks are injected."""

from __future__ import annotations

import os

import pytest

from attune.cli import build_parser, main
from attune.cli.brief_cmd import run_brief
from attune.cli.doctor import (
    FAIL,
    PASS,
    SKIP,
    WARN,
    Check,
    _fail_read,
    _fail_workspace,
    _qdrant_ready_url,
    check_audit_chain,
    check_channel_routes,
    check_data_dir,
    check_google_oauth_app,
    check_source_channels,
    run_doctor,
)
from attune.cli.init_cmd import (
    RECOMMENDED_EMBEDDING_DIMENSIONS,
    RECOMMENDED_EMBEDDING_MODEL,
    RECOMMENDED_MODELS,
    run_init,
)
from attune.cli.run_cmd import run_run


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def test_parser_knows_all_subcommands():
    parser = build_parser()
    for argv in (["doctor"], ["status", "--check"], ["repair", "--yes"],
                 ["brief"], ["brief", "--post"],
                 ["run", "--no-checks"], ["init", "--fresh"],
                 ["memory"], ["autonomy"], ["importance"]):
        args = parser.parse_args(argv)
        assert hasattr(args, "func")


def test_main_without_subcommand_prints_help_and_fails(capsys):
    assert main([]) == 1
    assert "attune" in capsys.readouterr().out


def test_autonomy_without_subcommand_prints_help(capsys):
    assert main(["autonomy"]) == 1
    assert "grant" in capsys.readouterr().out


def test_memory_without_subcommand_prints_help(capsys):
    assert main(["memory"]) == 1
    assert "remember" in capsys.readouterr().out


def test_importance_without_subcommand_prints_help(capsys):
    assert main(["importance"]) == 1
    assert "pin" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# init wizard
# ---------------------------------------------------------------------------


def _scripted(answers):
    """An ask() that pops scripted answers; '' means accept-default."""
    queue = list(answers)

    def ask(prompt):
        return queue.pop(0) if queue else ""

    return ask


def _by_prompt(values):
    def ask(prompt):
        return next((value for text, value in values.items() if text in prompt), "")
    return ask


def test_init_writes_env_from_scripted_answers(tmp_path):
    env_file = str(tmp_path / ".env")
    data_dir = str(tmp_path / "data")
    answers = {
        "Data directory": data_dir,
        "mailbox email": "owner@example.com",
        "Internal email domains": "example.com",
        "OpenAI-compatible base URL": "https://gateway.example/v1",
        "Default chat model": "general-model",
        "Classification model": "fast-model",
        "Memory extraction model": "fast-model",
        "Embedding model": "embed-model",
        "Embedding dimensions": "1536",
        "Google Cloud project ID": "attune-project",
        "Timezone": "America/Vancouver",
        "Morning brief time": "06:45",
    }
    lines: list[str] = []

    code = run_init(
        env_file=env_file,
        ask=_by_prompt(answers),
        ask_secret=lambda prompt: "secret-token" if "LLM API" in prompt else "",
        out=lines.append,
    )

    assert code == 0
    content = open(env_file).read()
    assert "ATTUNE_WORKSPACE_BACKEND=google_oauth" in content
    assert "ATTUNE_INGESTION_MODE=poll" in content
    assert f"ATTUNE_DATA_DIR={data_dir}" in content
    assert "ATTUNE_USER_ID=owner@example.com" in content
    assert "GOOGLE_PROJECT_ID=attune-project" in content
    assert "ATTUNE_LLM_API_KEY=secret-token" in content
    assert "ATTUNE_TIMEZONE=America/Vancouver" in content
    assert "ATTUNE_BRIEF_TIME=06:45" in content
    assert os.path.isdir(data_dir)
    assert oct(os.stat(env_file).st_mode & 0o777) == "0o600"
    # secrets are never echoed to output
    assert all("secret-token" not in line for line in lines)


def test_init_edits_existing_file_and_preserves_unknown_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ATTUNE_LLM_API_KEY=existing\nCUSTOM_SETTING=keep-me\n")
    out: list[str] = []

    code = run_init(env_file=str(env_file), ask=_scripted([]),
                    ask_secret=lambda p: "", out=out.append)

    assert code == 0
    assert "ATTUNE_LLM_API_KEY=existing" in env_file.read_text()
    assert "CUSTOM_SETTING=keep-me" in env_file.read_text()
    assert (tmp_path / ".env.bak").exists()


def test_init_migrates_legacy_names_and_removes_deployment_profile(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# keep this comment\n"
        "ADC_DEPLOYMENT=personal\n"
        "ADC_CONNECTOR_MODE=direct_oauth\n"
        "ADC_USER_ID=owner@example.com\n"
        "FUELIX_TOKEN=old-secret\n"
        "CUSTOM_SETTING=keep-me\n"
    )

    assert run_init(env_file=str(env_file), ask=_scripted([]),
                    ask_secret=lambda p: "", out=lambda s: None) == 0

    content = env_file.read_text()
    assert "# keep this comment" in content
    assert "ATTUNE_WORKSPACE_BACKEND=google_oauth" in content
    assert "ATTUNE_USER_ID=owner@example.com" in content
    assert "ATTUNE_LLM_API_KEY=old-secret" in content
    assert "CUSTOM_SETTING=keep-me" in content
    assert "ADC_" not in content
    assert "FUELIX_TOKEN" not in content
    assert "DEPLOYMENT=" not in content


def test_init_runs_oauth_flow_for_client_secret(tmp_path):
    secret_file = tmp_path / "client_secret.json"
    secret_file.write_text('{"installed": {"client_id": "x"}}')
    data_dir = str(tmp_path / "data")
    flows: list[dict] = []

    def fake_flow(*, client_secret_path, save_dir):
        flows.append({"path": client_secret_path, "dir": save_dir})
        return os.path.join(save_dir, "google_authorized_user.json")

    answers = {
        "Data directory": data_dir,
        "mailbox email": "owner@example.com",
        "Google Cloud project ID": "attune-project",
        "Google credentials JSON": str(secret_file),
        "Run Google consent flow": "y",
        "Default chat model": "test-model",
        "Embedding model": "test-embedding",
        "Embedding dimensions": "1536",
    }
    code = run_init(
        env_file=str(tmp_path / ".env"),
        ask=_by_prompt(answers),
        ask_secret=lambda p: "",
        oauth_flow=fake_flow,
        out=lambda s: None,
    )

    assert code == 0
    assert flows == [{"path": str(secret_file), "dir": data_dir}]
    content = (tmp_path / ".env").read_text()
    assert f"ATTUNE_GOOGLE_CREDENTIALS_FILE={data_dir}/google_authorized_user.json" in content


def test_init_offers_chat_oauth_flow_as_service_account_alternative(tmp_path):
    """Orgs that disallow creating IAM service-account keys can still wire
    Google Chat: `_chat_credentials_step` offers the same OAuth consent
    mechanism already used for Gmail/Calendar, scoped to Chat, saved to a
    distinct file (docs/deployment.md's Google Chat section)."""
    mailbox_secret = tmp_path / "mailbox_authorized_user.json"
    mailbox_secret.write_text('{"type": "authorized_user"}')
    chat_secret = tmp_path / "chat_client_secret.json"
    chat_secret.write_text('{"installed": {"client_id": "chat-app"}}')
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir)
    flows: list[dict] = []

    def fake_flow(*, client_secret_path, save_dir, scopes=None, filename=None):
        flows.append({
            "path": client_secret_path, "dir": save_dir,
            "scopes": scopes, "filename": filename,
        })
        return os.path.join(save_dir, filename or "google_authorized_user.json")

    answers = {
        "Data directory": data_dir,
        "mailbox email": "owner@example.com",
        "Google Cloud project ID": "attune-project",
        "Google credentials JSON": str(mailbox_secret),
        "Google Chat space": "spaces/AAAA",
        "Google Chat app service-account or OAuth JSON": str(chat_secret),
        "Run a Chat-scoped consent flow": "y",
        "Default chat model": "test-model",
        "Embedding model": "test-embedding",
        "Embedding dimensions": "1536",
    }
    code = run_init(
        env_file=str(tmp_path / ".env"),
        ask=_by_prompt(answers),
        ask_secret=lambda p: "",
        oauth_flow=fake_flow,
        out=lambda s: None,
    )

    assert code == 0
    assert flows == [{
        "path": str(chat_secret), "dir": data_dir,
        "scopes": ["https://www.googleapis.com/auth/chat.messages",
                   "https://www.googleapis.com/auth/chat.spaces.readonly"],
        "filename": "google_chat_authorized_user.json",
    }]
    content = (tmp_path / ".env").read_text()
    assert (
        f"ATTUNE_CHAT_CREDENTIALS_FILE={data_dir}/google_chat_authorized_user.json"
        in content
    )
    # distinct from the mailbox credential -- never the same file.
    assert f"ATTUNE_GOOGLE_CREDENTIALS_FILE={mailbox_secret}" in content


def test_init_offers_guided_google_setup_when_no_client_file_exists(tmp_path):
    data_dir = str(tmp_path / "data")
    calls = []

    def fake_google_setup(*, data_dir, ask, out):
        calls.append(data_dir)
        out("[fake checklist ran]")

    answers = {
        "Data directory": data_dir,
        "mailbox email": "owner@example.com",
        "Default chat model": "m",
        "Embedding model": "e",
        "Embedding dimensions": "1536",
        "guided Google Cloud setup checklist": "y",
    }
    lines = []
    code = run_init(
        env_file=str(tmp_path / ".env"),
        ask=_by_prompt(answers),
        ask_secret=lambda p: "",
        google_setup=fake_google_setup,
        out=lines.append,
    )

    assert code == 0
    assert calls == [data_dir]
    assert any("[fake checklist ran]" in line for line in lines)


def test_init_does_not_offer_google_setup_when_client_file_exists(tmp_path):
    secret_file = tmp_path / "client_secret.json"
    secret_file.write_text('{"installed": {"client_id": "x"}}')

    def fail_google_setup(**kwargs):
        raise AssertionError("must not offer the checklist when a file exists")

    answers = {
        "Data directory": str(tmp_path / "data"),
        "mailbox email": "owner@example.com",
        "Google credentials JSON": str(secret_file),
        "Run Google consent flow": "n",
        "Default chat model": "m",
        "Embedding model": "e",
        "Embedding dimensions": "1536",
    }
    code = run_init(
        env_file=str(tmp_path / ".env"),
        ask=_by_prompt(answers),
        ask_secret=lambda p: "",
        google_setup=fail_google_setup,
        out=lambda s: None,
    )
    assert code == 0


def test_quick_init_asks_only_the_essential_questions(tmp_path):
    data_dir = str(tmp_path / "data")
    essential_substrings = (
        "Workspace backend",
        "Data directory",
        "mailbox email",
        "Internal email domains",
        "OpenAI-compatible base URL",
        "LLM API key",
        "Default chat model",
        "Embedding API key",
        "Embedding model",
    )

    def ask(prompt: str) -> str:
        assert any(text in prompt for text in essential_substrings), (
            f"quick mode asked a non-essential question: {prompt!r}"
        )
        if "Data directory" in prompt:
            return data_dir
        if "mailbox email" in prompt:
            return "owner@example.com"
        return ""

    def ask_secret(prompt: str) -> str:
        assert any(text in prompt for text in essential_substrings), (
            f"quick mode asked a non-essential secret: {prompt!r}"
        )
        return "secret" if "LLM API" in prompt else ""

    lines = []
    code = run_init(
        env_file=str(tmp_path / ".env"),
        quick=True,
        ask=ask,
        ask_secret=ask_secret,
        out=lines.append,
    )

    assert code == 0
    assert any("Quick setup skipped" in line for line in lines)
    assert any("attune init --google-setup" in line for line in lines)
    content = (tmp_path / ".env").read_text()
    env_lines = content.splitlines()
    assert "ATTUNE_SLACK_CHANNEL=" in env_lines  # channels unset = disabled
    # per-task overrides defaulted blank => falls back to ATTUNE_MODEL_DEFAULT
    assert "ATTUNE_MODEL_CLASSIFY=" in env_lines


def test_quick_init_preserves_existing_non_essential_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ATTUNE_MODEL_DRAFT=already-configured-model\n"
        "SLACK_BOT_TOKEN=xoxb-existing\n"
        "ATTUNE_SLACK_CHANNEL=U0123456789\n"
        "ATTUNE_TIMEZONE=America/Vancouver\n"
    )
    data_dir = str(tmp_path / "data")
    answers = {
        "Data directory": data_dir,
        "mailbox email": "owner@example.com",
        "Default chat model": "m",
        "Embedding model": "e",
        "Embedding dimensions": "1536",
    }
    code = run_init(
        env_file=str(env_file),
        quick=True,
        ask=_by_prompt(answers),
        ask_secret=lambda p: "",
        out=lambda s: None,
    )

    assert code == 0
    content = env_file.read_text()
    assert "ATTUNE_MODEL_DRAFT=already-configured-model" in content
    assert "SLACK_BOT_TOKEN=xoxb-existing" in content
    assert "ATTUNE_SLACK_CHANNEL=U0123456789" in content
    assert "ATTUNE_TIMEZONE=America/Vancouver" in content


def test_recommended_fills_documented_values_on_a_new_setup(tmp_path):
    data_dir = str(tmp_path / "data")
    answers = {
        "Data directory": data_dir,
        "mailbox email": "owner@example.com",
    }
    code = run_init(
        env_file=str(tmp_path / ".env"),
        quick=True,
        recommended=True,
        ask=_by_prompt(answers),
        ask_secret=lambda p: "secret",
        out=lambda s: None,
    )

    assert code == 0
    content = (tmp_path / ".env").read_text()
    for key, value in RECOMMENDED_MODELS.items():
        assert f"{key}={value}" in content
    assert f"ATTUNE_EMBEDDING_MODEL={RECOMMENDED_EMBEDDING_MODEL}" in content
    assert f"ATTUNE_EMBEDDING_DIMENSIONS={RECOMMENDED_EMBEDDING_DIMENSIONS}" in content


def test_recommended_does_not_override_an_explicit_existing_value(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ATTUNE_MODEL_DRAFT=custom-model\n")
    data_dir = str(tmp_path / "data")
    answers = {
        "Data directory": data_dir,
        "mailbox email": "owner@example.com",
        "Default chat model": "m",
        "Embedding model": "e",
        "Embedding dimensions": "1536",
    }
    code = run_init(
        env_file=str(env_file),
        recommended=True,
        ask=_by_prompt(answers),
        ask_secret=lambda p: "",
        out=lambda s: None,
    )

    assert code == 0
    content = env_file.read_text()
    assert "ATTUNE_MODEL_DRAFT=custom-model" in content
    # An override left blank still picks up the recommended value.
    assert f"ATTUNE_MODEL_CLASSIFY={RECOMMENDED_MODELS['ATTUNE_MODEL_CLASSIFY']}" in content


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_renders_statuses_and_exit_code():
    lines: list[str] = []
    checks = [
        Check("env", lambda: (PASS, "workspace=google_oauth")),
        Check("llm", lambda: (FAIL, "ATTUNE_LLM_API_KEY not set — add it to .env")),
        Check("slack", lambda: (SKIP, "SLACK_BOT_TOKEN not set")),
    ]

    code = run_doctor(checks, out=lines.append)

    assert code == 1
    assert any(line.startswith("PASS") and "env" in line for line in lines)
    assert any(line.startswith("FAIL") and "ATTUNE_LLM_API_KEY" in line for line in lines)
    assert any(line.startswith("SKIP") for line in lines)
    assert any("1 check(s) FAILED" in line for line in lines)


def test_doctor_warning_is_nonfatal_and_summarized():
    lines: list[str] = []
    checks = [
        Check("env", lambda: (PASS, "ok")),
        Check("python", lambda: (WARN, "upgrade recommended")),
    ]

    code = run_doctor(checks, out=lines.append)

    assert code == 0
    assert any(line.startswith("WARN") for line in lines)
    assert any("required checks passed" in line for line in lines)


def test_doctor_crashing_check_is_a_fail_not_a_traceback():
    def boom():
        raise RuntimeError("network down")

    lines: list[str] = []
    code = run_doctor([Check("gmail-read", boom)], out=lines.append)

    assert code == 1
    assert any("RuntimeError" in line for line in lines)


def test_doctor_fatal_only_filters_battery():
    ran: list[str] = []

    def mk(name):
        return Check(name, lambda: (ran.append(name), (PASS, "ok"))[1])

    checks = [mk("env"), mk("llm"), mk("channels"), mk("slack"), mk("pubsub")]
    run_doctor(checks, out=lambda s: None, fatal_only=True)

    assert ran == ["env", "llm", "channels"]  # network channel checks aren't fatal


def test_doctor_qdrant_target_uses_resolved_settings():
    from attune.config import Settings

    settings = Settings.from_env({
        "ATTUNE_QDRANT_HOST": "qdrant",
        "ATTUNE_QDRANT_PORT": "7333",
    })
    assert _qdrant_ready_url(settings) == "http://qdrant:7333/readyz"


def test_channel_routes_skip_when_no_surface_is_selected():
    from attune.config import Settings

    assert check_channel_routes(Settings.from_env({}))[0] == SKIP


def test_channel_routes_fail_with_actionable_missing_slack_settings():
    from attune.config import Settings

    status, detail = check_channel_routes(Settings.from_env({
        "ATTUNE_BRIEF_CHANNELS": "slack",
        "ATTUNE_INTERACTION_CHANNELS": "slack",
    }))

    assert status == FAIL
    assert "SLACK_BOT_TOKEN" in detail
    assert "ATTUNE_SLACK_CHANNEL" in detail
    assert "SLACK_APP_TOKEN" in detail
    assert "ATTUNE_SLACK_ALLOWED_USERS" in detail


def test_channel_routes_accept_complete_google_chat_only_configuration():
    from attune.config import Settings

    settings = Settings.from_env({
        "ATTUNE_BRIEF_CHANNELS": "google_chat",
        "ATTUNE_APPROVAL_CHANNEL": "google_chat",
        "ATTUNE_NOTIFICATION_CHANNELS": "google_chat",
        "ATTUNE_INTERACTION_CHANNELS": "google_chat",
        "ATTUNE_CHAT_SPACE": "spaces/S1",
        "ATTUNE_CHAT_CREDENTIALS_FILE": "/secrets/chat.json",
        "ATTUNE_CHAT_ALLOWED_USERS": "users/U1",
        "ATTUNE_CHAT_INTERACTION_PUBSUB_SUBSCRIPTION": "projects/p/subscriptions/chat",
        "ATTUNE_ACK_DESTINATION_VISIBILITY": "1",
    })

    assert check_channel_routes(settings)[0] == PASS


_COMPLETE_GOOGLE_CHAT_CONFIG = {
    "ATTUNE_BRIEF_CHANNELS": "google_chat",
    "ATTUNE_APPROVAL_CHANNEL": "google_chat",
    "ATTUNE_NOTIFICATION_CHANNELS": "google_chat",
    "ATTUNE_INTERACTION_CHANNELS": "google_chat",
    "ATTUNE_CHAT_SPACE": "spaces/S1",
    "ATTUNE_CHAT_ALLOWED_USERS": "users/U1",
    "ATTUNE_CHAT_INTERACTION_PUBSUB_SUBSCRIPTION": "projects/p/subscriptions/chat",
    "ATTUNE_ACK_DESTINATION_VISIBILITY": "1",
}


def test_channel_routes_pass_when_chat_credential_differs_from_google_credential():
    """A distinct Chat OAuth-user credential (the service-account
    alternative, credentials.py) is fine alongside the principal's own
    Google credentials file, as long as the two paths differ."""
    from attune.config import Settings

    settings = Settings.from_env({
        **_COMPLETE_GOOGLE_CHAT_CONFIG,
        "ATTUNE_CHAT_CREDENTIALS_FILE": "/secrets/chat_authorized_user.json",
        "ATTUNE_GOOGLE_CREDENTIALS_FILE": "/secrets/mailbox_authorized_user.json",
    })

    assert check_channel_routes(settings)[0] == PASS


def test_channel_routes_fail_when_chat_credential_is_the_google_credential():
    """design.md rule 4: the Chat app identity must never be the same
    credential as the principal's Gmail/Calendar OAuth grant, regardless of
    whether it's a service account or (credentials.py's alternative) an
    OAuth user credential."""
    from attune.config import Settings

    settings = Settings.from_env({
        **_COMPLETE_GOOGLE_CHAT_CONFIG,
        "ATTUNE_CHAT_CREDENTIALS_FILE": "/secrets/shared.json",
        "ATTUNE_GOOGLE_CREDENTIALS_FILE": "/secrets/shared.json",
    })

    status, detail = check_channel_routes(settings)
    assert status == FAIL
    assert "must not be the same file" in detail


# ---------------------------------------------------------------------------
# source-channels (Phase 2 stage 1, docs/future-state.md; G1/G3)
# ---------------------------------------------------------------------------


def test_source_channels_skip_when_none_configured():
    from attune.config import Settings

    assert check_source_channels(Settings.from_env({}))[0] == SKIP


def test_source_channels_fail_without_slack_bot_token():
    from attune.config import Settings

    settings = Settings.from_env({"ATTUNE_SLACK_SOURCE_CHANNELS": "C111"})
    status, detail = check_source_channels(settings)
    assert status == FAIL
    assert "SLACK_BOT_TOKEN" in detail


def test_source_channels_fail_without_chat_credentials():
    from attune.config import Settings

    settings = Settings.from_env({"ATTUNE_CHAT_SOURCE_SPACES": "spaces/AAAA"})
    status, detail = check_source_channels(settings)
    assert status == FAIL
    assert "ATTUNE_CHAT_CREDENTIALS_FILE" in detail


def test_source_channels_fail_reports_both_missing_credentials():
    from attune.config import Settings

    settings = Settings.from_env({
        "ATTUNE_SLACK_SOURCE_CHANNELS": "C111",
        "ATTUNE_CHAT_SOURCE_SPACES": "spaces/AAAA",
    })
    status, detail = check_source_channels(settings)
    assert status == FAIL
    assert "SLACK_BOT_TOKEN" in detail
    assert "ATTUNE_CHAT_CREDENTIALS_FILE" in detail


def test_source_channels_pass_with_full_configuration():
    from attune.config import Settings

    settings = Settings.from_env({
        "ATTUNE_SLACK_SOURCE_CHANNELS": "C111",
        "ATTUNE_CHAT_SOURCE_SPACES": "spaces/AAAA",
        "SLACK_BOT_TOKEN": "xoxb-...",
        "ATTUNE_CHAT_CREDENTIALS_FILE": "/secrets/chat.json",
    })
    assert check_source_channels(settings)[0] == PASS


def test_source_channels_unrelated_to_interaction_allowlists():
    """A source channel needs no interaction allowlist at all — the whole
    point of the distinction (see ingestion/sources.py's module docstring)."""
    from attune.config import Settings

    settings = Settings.from_env({
        "ATTUNE_SLACK_SOURCE_CHANNELS": "C111",
        "SLACK_BOT_TOKEN": "xoxb-...",
        # No ATTUNE_SLACK_ALLOWED_USERS, no interaction channels configured.
    })
    assert check_source_channels(settings)[0] == PASS


def test_source_channels_is_a_fatal_check():
    from attune.cli.doctor import FATAL_CHECKS

    assert "source-channels" in FATAL_CHECKS


# ---------------------------------------------------------------------------
# mail-labels (Phase 3 stage 1, docs/future-state.md; G9)
# ---------------------------------------------------------------------------


def test_mail_labels_skip_when_disabled():
    from attune.cli.doctor import check_mail_labels
    from attune.config import Settings

    settings = Settings.from_env({})
    status, detail = check_mail_labels(settings)
    assert status == SKIP
    assert "ATTUNE_MAIL_LABELS_ENABLED" in detail


def test_mail_labels_fail_on_mcp_backend():
    from attune.cli.doctor import check_mail_labels
    from attune.config import Settings

    settings = Settings.from_env({
        "ATTUNE_MAIL_LABELS_ENABLED": "1",
        "ATTUNE_WORKSPACE_BACKEND": "mcp",
        "ATTUNE_MCP_URL": "https://mcp.example/mcp",
    })
    status, detail = check_mail_labels(settings)
    assert status == FAIL
    assert "MCP" in detail
    assert "contract v1" in detail


def test_mail_labels_pass_on_google_oauth_backend():
    from attune.cli.doctor import check_mail_labels
    from attune.config import Settings

    settings = Settings.from_env({
        "ATTUNE_MAIL_LABELS_ENABLED": "1",
        "ATTUNE_WORKSPACE_BACKEND": "google_oauth",
    })
    status, detail = check_mail_labels(settings)
    assert status == PASS
    assert "gmail.modify" in detail


def test_mail_labels_is_a_fatal_check():
    from attune.cli.doctor import FATAL_CHECKS

    assert "mail-labels" in FATAL_CHECKS


# ---------------------------------------------------------------------------
# calendar-writes (Phase 3 stage 2)
# ---------------------------------------------------------------------------


def test_calendar_writes_skip_when_disabled():
    from attune.cli.doctor import check_calendar_writes
    from attune.config import Settings

    settings = Settings.from_env({})
    status, detail = check_calendar_writes(settings)
    assert status == SKIP
    assert "ATTUNE_CALENDAR_WRITES_ENABLED" in detail


def test_calendar_writes_fail_on_mcp_backend():
    from attune.cli.doctor import check_calendar_writes
    from attune.config import Settings

    settings = Settings.from_env({
        "ATTUNE_CALENDAR_WRITES_ENABLED": "1",
        "ATTUNE_WORKSPACE_BACKEND": "mcp",
        "ATTUNE_MCP_URL": "https://mcp.example/mcp",
    })
    status, detail = check_calendar_writes(settings)
    assert status == FAIL
    assert "MCP" in detail
    assert "contract v1" in detail


def test_calendar_writes_pass_on_google_oauth_backend():
    from attune.cli.doctor import check_calendar_writes
    from attune.config import Settings

    settings = Settings.from_env({
        "ATTUNE_CALENDAR_WRITES_ENABLED": "1",
        "ATTUNE_WORKSPACE_BACKEND": "google_oauth",
    })
    status, detail = check_calendar_writes(settings)
    assert status == PASS
    assert "calendar.events" in detail


def test_calendar_writes_is_a_fatal_check():
    from attune.cli.doctor import FATAL_CHECKS

    assert "calendar-writes" in FATAL_CHECKS


# ---------------------------------------------------------------------------
# mail-send (Phase 4 stage 2, docs/future-state.md; G15)
# ---------------------------------------------------------------------------


def test_mail_send_skip_when_disabled():
    from attune.cli.doctor import check_mail_send
    from attune.config import Settings

    settings = Settings.from_env({})
    status, detail = check_mail_send(settings)
    assert status == SKIP
    assert "ATTUNE_MAIL_SEND_ENABLED" in detail


def test_mail_send_fail_on_mcp_backend():
    from attune.cli.doctor import check_mail_send
    from attune.config import Settings

    settings = Settings.from_env({
        "ATTUNE_MAIL_SEND_ENABLED": "1",
        "ATTUNE_WORKSPACE_BACKEND": "mcp",
        "ATTUNE_MCP_URL": "https://mcp.example/mcp",
    })
    status, detail = check_mail_send(settings)
    assert status == FAIL
    assert "MCP" in detail
    assert "contract v1" in detail


def test_mail_send_pass_on_google_oauth_backend():
    from attune.cli.doctor import check_mail_send
    from attune.config import Settings

    settings = Settings.from_env({
        "ATTUNE_MAIL_SEND_ENABLED": "1",
        "ATTUNE_WORKSPACE_BACKEND": "google_oauth",
    })
    status, detail = check_mail_send(settings)
    assert status == PASS
    assert "gmail.send" in detail


def test_mail_send_is_a_fatal_check():
    from attune.cli.doctor import FATAL_CHECKS

    assert "mail-send" in FATAL_CHECKS


# ---------------------------------------------------------------------------
# audit-chain (security finding F1)
# ---------------------------------------------------------------------------


def test_audit_chain_skips_when_file_absent(tmp_path):
    from attune.config import Settings

    settings = Settings.from_env({
        "ATTUNE_AUDIT_LOG_PATH": str(tmp_path / "audit.log.jsonl"),
    })
    status, detail = check_audit_chain(settings)
    assert status == SKIP
    assert "audit.log.jsonl" in detail


def test_audit_chain_passes_on_clean_chained_file(tmp_path):
    from attune.audit.log import JsonlAuditLog
    from attune.config import Settings

    path = tmp_path / "audit.log.jsonl"
    log = JsonlAuditLog(str(path))
    log.record(thread_id="t1", workflow="w", events=[
        {"event": "a", "ts": "2026-07-10T00:00:00+00:00"},
        {"event": "b", "ts": "2026-07-10T00:00:01+00:00"},
    ])

    settings = Settings.from_env({"ATTUNE_AUDIT_LOG_PATH": str(path)})
    status, detail = check_audit_chain(settings)
    assert status == PASS
    assert "2 hashed" in detail


def test_audit_chain_fails_with_line_number_on_tamper(tmp_path):
    import json

    from attune.audit.log import JsonlAuditLog
    from attune.config import Settings

    path = tmp_path / "audit.log.jsonl"
    log = JsonlAuditLog(str(path))
    log.record(thread_id="t1", workflow="w", events=[
        {"event": "a", "ts": "2026-07-10T00:00:00+00:00"},
        {"event": "b", "ts": "2026-07-10T00:00:01+00:00"},
    ])
    lines = [json.loads(line) for line in path.read_text().strip().split("\n")]
    lines[1]["event"] = "tampered"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

    settings = Settings.from_env({"ATTUNE_AUDIT_LOG_PATH": str(path)})
    status, detail = check_audit_chain(settings)
    assert status == FAIL
    assert "line 2" in detail


# ---------------------------------------------------------------------------
# data-dir (security finding F5): fatal check, fail closed on unset
# ATTUNE_DATA_DIR
# ---------------------------------------------------------------------------


def test_data_dir_fails_when_unset():
    from attune.config import Settings

    settings = Settings.from_env({})
    assert settings.data_dir is None  # sanity: this is the unset case

    status, detail = check_data_dir(settings)
    assert status == FAIL
    assert "ATTUNE_DATA_DIR" in detail


def test_data_dir_passes_and_corrects_permissions(tmp_path):
    from attune.config import Settings

    target = tmp_path / "attune-data"
    settings = Settings.from_env({"ATTUNE_DATA_DIR": str(target)})

    status, detail = check_data_dir(settings)

    assert status == PASS
    assert str(target) in detail
    assert os.path.isdir(target)
    assert (os.stat(target).st_mode & 0o777) == 0o700


def test_data_dir_corrects_overly_permissive_existing_directory(tmp_path):
    from attune.config import Settings

    target = tmp_path / "attune-data"
    target.mkdir()
    os.chmod(target, 0o777)

    settings = Settings.from_env({"ATTUNE_DATA_DIR": str(target)})
    status, _ = check_data_dir(settings)

    assert status == PASS
    assert (os.stat(target).st_mode & 0o777) == 0o700


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses directory permission checks",
)
def test_data_dir_fails_when_not_writable(tmp_path):
    from attune.config import Settings

    parent = tmp_path / "readonly-parent"
    parent.mkdir(mode=0o500)
    target = parent / "attune-data"
    settings = Settings.from_env({"ATTUNE_DATA_DIR": str(target)})

    try:
        status, detail = check_data_dir(settings)
        assert status == FAIL
        assert "ATTUNE_DATA_DIR" in detail
    finally:
        os.chmod(parent, 0o700)  # allow tmp_path cleanup


def test_data_dir_is_in_fatal_checks():
    """attune run's preflight (run_cmd.run_run -> run_doctor(fatal_only=True))
    only re-runs checks named in FATAL_CHECKS — confirm data-dir is one of
    them, which is what makes an unset ATTUNE_DATA_DIR actually block
    `attune run` rather than merely warn in the full battery."""
    from attune.cli.doctor import FATAL_CHECKS

    assert "data-dir" in FATAL_CHECKS


# ---------------------------------------------------------------------------
# google-oauth-app (UX item #2, G20) + inline Doctor fix hints (UX item #4)
# ---------------------------------------------------------------------------


def test_google_oauth_app_skip_when_mcp_backend():
    from attune.config import Settings

    settings = Settings.from_env({
        "ATTUNE_WORKSPACE_BACKEND": "mcp",
        "ATTUNE_MCP_URL": "https://mcp.example/mcp",
    })
    status, detail = check_google_oauth_app(settings)
    assert status == SKIP
    assert "mcp" in detail


def test_google_oauth_app_skip_when_no_state_recorded(tmp_path):
    from attune.config import Settings

    settings = Settings.from_env({"ATTUNE_DATA_DIR": str(tmp_path)})
    status, detail = check_google_oauth_app(settings)
    assert status == SKIP
    assert "google-setup" in detail or "legacy" in detail


def test_google_oauth_app_skip_when_internal(tmp_path):
    from attune.cli.google_setup_state import GoogleSetupState, google_setup_state_path
    from attune.config import Settings

    state = GoogleSetupState()
    state.set_consent_mode("internal")
    state.save(google_setup_state_path(str(tmp_path)))

    settings = Settings.from_env({"ATTUNE_DATA_DIR": str(tmp_path)})
    status, detail = check_google_oauth_app(settings)
    assert status == SKIP
    assert "internal" in detail


def test_google_oauth_app_skip_when_published():
    from attune.cli.google_setup_state import GoogleSetupState, google_setup_state_path
    from attune.config import Settings

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        state = GoogleSetupState()
        state.set_consent_mode("external_published")
        state.save(google_setup_state_path(tmp))
        settings = Settings.from_env({"ATTUNE_DATA_DIR": tmp})
        status, detail = check_google_oauth_app(settings)
    assert status == SKIP


def test_google_oauth_app_warns_on_external_testing_with_credential_age(tmp_path):
    from attune.cli.google_setup_state import GoogleSetupState, google_setup_state_path
    from attune.config import Settings

    state = GoogleSetupState()
    state.set_consent_mode("external_testing")
    state.save(google_setup_state_path(str(tmp_path)))
    cred_file = tmp_path / "google_authorized_user.json"
    cred_file.write_text("{}")

    settings = Settings.from_env({
        "ATTUNE_DATA_DIR": str(tmp_path),
        "ATTUNE_GOOGLE_CREDENTIALS_FILE": str(cred_file),
    })
    status, detail = check_google_oauth_app(settings)
    assert status == WARN
    assert "7 days" in detail
    assert "Internal" in detail
    assert "publish" in detail
    assert "day(s) old" in detail


def test_google_oauth_app_is_not_a_fatal_check():
    from attune.cli.doctor import FATAL_CHECKS

    assert "google-oauth-app" not in FATAL_CHECKS


def test_fail_workspace_hint_for_google_oauth_backend():
    status, detail = _fail_workspace(FileNotFoundError("no such file"), mcp=False)
    assert status == FAIL
    assert "ATTUNE_GOOGLE_CREDENTIALS_FILE" in detail
    assert "authorized-user JSON" in detail
    assert "attune init" in detail


def test_fail_workspace_hint_for_mcp_backend():
    status, detail = _fail_workspace(ConnectionError("refused"), mcp=True)
    assert status == FAIL
    assert "tools/list" in detail
    assert "mcp-contract.md" in detail


def test_fail_workspace_appends_invalid_grant_reauth_hint():
    status, detail = _fail_workspace(Exception("invalid_grant: Token has expired"), mcp=False)
    assert status == FAIL
    assert "7 days" in detail
    assert "attune init --google-setup" in detail


def test_fail_read_hint_names_the_source_and_fix():
    status, detail = _fail_read(Exception("boom"), source="Gmail")
    assert status == FAIL
    assert "enable the Gmail API" in detail
    assert "add the test user" in detail
    assert "attune init" in detail


def test_fail_read_appends_invalid_grant_reauth_hint():
    status, detail = _fail_read(Exception("invalid_grant"), source="Calendar")
    assert status == FAIL
    assert "7 days" in detail
    assert "attune init --google-setup" in detail


# ---------------------------------------------------------------------------
# brief + run
# ---------------------------------------------------------------------------


class _FakeMsg:
    def __init__(self, c):
        self.message = type("M", (), {"content": c})


class _FakeResp:
    def __init__(self, c):
        self.choices = [_FakeMsg(c)]


class _FakeClient:
    def chat_completions_create(self, **kw):
        return _FakeResp("All quiet today.")


class _FakeConnector:
    def list_threads(self, query="is:unread", *, max_results=20):
        return []

    def list_events(self, *, time_min, time_max):
        return []


def test_brief_prints_assembled_summary(capsys):
    from attune.config import Settings

    settings = Settings.from_env({"ATTUNE_MEM0_URL": ""})
    code = run_brief(build=lambda: (_FakeConnector(), _FakeClient(), settings))

    assert code == 0
    out = capsys.readouterr().out
    assert "All quiet today." in out
    assert "0 unread" in out


def test_plain_brief_does_not_load_google_credentials_for_mcp(monkeypatch):
    from attune.cli.brief_cmd import _default_build
    from attune.config import Settings

    settings = Settings.from_env({
        "ATTUNE_WORKSPACE_BACKEND": "mcp",
        "ATTUNE_MCP_URL": "https://mcp.example/mcp",
    })
    connector = object()
    client = object()

    def fail_google_load(configured):
        raise AssertionError("Google credentials should not load for MCP")

    monkeypatch.setattr(
        "attune.config.Settings.from_env", classmethod(lambda cls: settings)
    )
    monkeypatch.setattr(
        "attune.credentials.load_google_credentials",
        fail_google_load,
    )
    monkeypatch.setattr(
        "attune.connectors.make_connector",
        lambda configured, **kwargs: connector,
    )
    monkeypatch.setattr(
        "attune.llm.make_client",
        lambda **kwargs: client,
    )

    assert _default_build() == (connector, client, settings)


def test_run_gates_on_fatal_doctor_checks():
    started: list[bool] = []

    class _FakeRuntime:
        def run(self):
            started.append(True)

    code = run_run(
        doctor=lambda **kw: 1,
        runtime_factory=lambda: _FakeRuntime(),
        out=lambda s: None,
    )
    assert code == 1
    assert started == []

    code = run_run(
        doctor=lambda **kw: 0,
        runtime_factory=lambda: _FakeRuntime(),
        out=lambda s: None,
    )
    assert code == 0
    assert started == [True]


def test_run_no_checks_skips_doctor():
    started: list[bool] = []

    class _FakeRuntime:
        def run(self):
            started.append(True)

    def never(**kw):
        raise AssertionError("doctor must not run with --no-checks")

    code = run_run(no_checks=True, doctor=never,
                   runtime_factory=lambda: _FakeRuntime(), out=lambda s: None)
    assert code == 0
    assert started == [True]
