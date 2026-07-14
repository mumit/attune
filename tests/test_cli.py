"""Tests for the CLI (roadmap prompt 08) — wizard, doctor, brief, parser.
Everything offline: prompts/secrets/flows/checks are injected."""

from __future__ import annotations

import os

from attune.cli import build_parser, main
from attune.cli.brief_cmd import run_brief
from attune.cli.doctor import (
    FAIL,
    PASS,
    SKIP,
    WARN,
    Check,
    check_channel_routes,
    run_doctor,
)
from attune.cli.init_cmd import run_init
from attune.cli.run_cmd import run_run


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def test_parser_knows_all_subcommands():
    parser = build_parser()
    for argv in (["doctor"], ["brief"], ["brief", "--post"],
                 ["run", "--no-checks"], ["init", "--fresh"],
                 ["memory"], ["autonomy"]):
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
