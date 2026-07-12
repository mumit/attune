"""Tests for the CLI (roadmap prompt 08) — wizard, doctor, brief, parser.
Everything offline: prompts/secrets/flows/checks are injected."""

from __future__ import annotations

import os

from aidedecamp.cli import build_parser, main
from aidedecamp.cli.brief_cmd import run_brief
from aidedecamp.cli.doctor import FAIL, PASS, SKIP, Check, run_doctor
from aidedecamp.cli.init_cmd import run_init
from aidedecamp.cli.run_cmd import run_run


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def test_parser_knows_all_subcommands():
    parser = build_parser()
    for argv in (["doctor"], ["brief"], ["brief", "--post"],
                 ["run", "--no-checks"], ["init", "--force"],
                 ["memory"], ["autonomy"]):
        args = parser.parse_args(argv)
        assert hasattr(args, "func")


def test_main_without_subcommand_prints_help_and_fails(capsys):
    assert main([]) == 1
    assert "aidedecamp" in capsys.readouterr().out


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


def test_init_writes_env_from_scripted_answers(tmp_path):
    env_file = str(tmp_path / ".env")
    data_dir = str(tmp_path / "data")
    answers = [
        "",                # deployment -> personal
        "",                # connector -> direct_oauth
        "poll",            # ingestion
        data_dir,          # data dir
        "owner@example.com", # mailbox principal
        "adc-project",     # Google Cloud project
        "",                # google credentials -> blank (ADC)
        "",                # chat space -> skip
        "America/Vancouver",
        "06:45",
    ]
    secrets = iter(["fuelix-secret-token", "", ""])  # fuelix, slack bot (skip)
    lines: list[str] = []

    code = run_init(
        env_file=env_file,
        ask=_scripted(answers),
        ask_secret=lambda prompt: next(secrets),
        out=lines.append,
    )

    assert code == 0
    content = open(env_file).read()
    assert "ADC_DEPLOYMENT=personal" in content
    assert "ADC_CONNECTOR_MODE=direct_oauth" in content
    assert "ADC_INGESTION_MODE=poll" in content
    assert f"ADC_DATA_DIR={data_dir}" in content
    assert "ADC_USER_ID=owner@example.com" in content
    assert "GOOGLE_PROJECT_ID=adc-project" in content
    assert "FUELIX_TOKEN=fuelix-secret-token" in content
    assert "ADC_TIMEZONE=America/Vancouver" in content
    assert "ADC_BRIEF_TIME=06:45" in content
    assert os.path.isdir(data_dir)
    assert oct(os.stat(env_file).st_mode & 0o777) == "0o600"
    # secrets are never echoed to output
    assert all("fuelix-secret-token" not in line for line in lines)


def test_init_refuses_overwrite_without_force(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("FUELIX_TOKEN=existing")
    out: list[str] = []

    code = run_init(env_file=str(env_file), ask=_scripted([]),
                    ask_secret=lambda p: "", out=out.append)

    assert code == 1
    assert env_file.read_text() == "FUELIX_TOKEN=existing"
    assert any("--force" in line for line in out)


def test_init_runs_oauth_flow_for_client_secret(tmp_path):
    secret_file = tmp_path / "client_secret.json"
    secret_file.write_text('{"installed": {"client_id": "x"}}')
    data_dir = str(tmp_path / "data")
    flows: list[dict] = []

    def fake_flow(*, client_secret_path, save_dir):
        flows.append({"path": client_secret_path, "dir": save_dir})
        return os.path.join(save_dir, "google_authorized_user.json")

    answers = [
        "", "", "poll", data_dir, "owner@example.com", "adc-project",
        str(secret_file),  # google credentials -> client secret
        "y",               # run the consent flow
        "", "", "",        # chat space, tz, brief time defaults
    ]
    code = run_init(
        env_file=str(tmp_path / ".env"),
        ask=_scripted(answers),
        ask_secret=lambda p: "",
        oauth_flow=fake_flow,
        out=lambda s: None,
    )

    assert code == 0
    assert flows == [{"path": str(secret_file), "dir": data_dir}]
    content = (tmp_path / ".env").read_text()
    assert f"ADC_GOOGLE_CREDENTIALS_FILE={data_dir}/google_authorized_user.json" in content


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_renders_statuses_and_exit_code():
    lines: list[str] = []
    checks = [
        Check("env", lambda: (PASS, "deployment=personal")),
        Check("fuelix", lambda: (FAIL, "FUELIX_TOKEN not set — add it to .env")),
        Check("slack", lambda: (SKIP, "SLACK_BOT_TOKEN not set")),
    ]

    code = run_doctor(checks, out=lines.append)

    assert code == 1
    assert any(line.startswith("PASS") and "env" in line for line in lines)
    assert any(line.startswith("FAIL") and "FUELIX_TOKEN" in line for line in lines)
    assert any(line.startswith("SKIP") for line in lines)
    assert any("1 check(s) FAILED" in line for line in lines)


def test_doctor_all_green_exits_zero():
    code = run_doctor([Check("env", lambda: (PASS, "ok"))], out=lambda s: None)
    assert code == 0


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

    checks = [mk("env"), mk("fuelix"), mk("slack"), mk("pubsub")]
    run_doctor(checks, out=lambda s: None, fatal_only=True)

    assert ran == ["env", "fuelix"]  # slack/pubsub aren't fatal


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
    from aidedecamp.config import Settings

    settings = Settings.from_env({"ADC_MEM0_URL": ""})
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
