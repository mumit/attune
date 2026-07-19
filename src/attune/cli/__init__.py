"""The ``attune`` command-line interface (roadmap prompt 08).

Until this existed, the only entrypoint was ``python -m attune`` — which
immediately needs a fully configured environment and live GCP — and setup was
a 600-line manual runbook. The CLI is the human front door:

    attune init      interactive setup wizard (writes .env)
    attune doctor    validate every credential/resource, with fix hints
    attune status    inspect secret-free setup progress and live health
    attune repair    reapply the recorded local plan and validate it
    attune brief     assemble one morning brief and print it
    attune run       start the always-on process (doctor-gated)
    attune memory    (subcommand group — arrives with roadmap M4)
    attune autonomy  (subcommand group — arrives with roadmap M4)

Stdlib ``argparse`` — a CLI with five subcommands doesn't justify a click/
typer dependency. Heavy imports happen inside subcommands so
``attune --help`` works in a bare install.
"""

from __future__ import annotations

import argparse
import warnings
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="attune",
        description="A self-learning workspace assistant over Gmail, "
        "Calendar, Google Chat, and Slack.",
    )
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser(
        "init", help="interactive setup: write .env, bootstrap Google OAuth"
    )
    p_init.add_argument("--env-file", default=".env", help="where to write settings")
    p_init.add_argument(
        "--fresh", action="store_true", help="ignore existing values and create a fresh env file"
    )
    p_init.add_argument(
        "--target",
        choices=("configure", "local"),
        default="configure",
        help="configure only (default), or provision and validate local Qdrant",
    )
    p_init.add_argument(
        "--yes",
        action="store_true",
        help="apply the displayed deterministic deployment plan without prompting",
    )
    p_init.set_defaults(func=_cmd_init)

    p_doctor = sub.add_parser(
        "doctor", help="validate configuration, credentials, and services"
    )
    p_doctor.set_defaults(func=_cmd_doctor)

    p_status = sub.add_parser(
        "status", help="show secret-free setup progress and optionally run Doctor"
    )
    p_status.add_argument(
        "--env-file", default=".env", help="configured environment file"
    )
    p_status.add_argument(
        "--check", action="store_true", help="also run the live Doctor battery"
    )
    p_status.set_defaults(func=_cmd_status)

    p_repair = sub.add_parser(
        "repair", help="reapply and validate the recorded local deployment"
    )
    p_repair.add_argument(
        "--env-file", default=".env", help="configured environment file"
    )
    p_repair.add_argument(
        "--yes",
        action="store_true",
        help="apply the displayed deterministic repair plan without prompting",
    )
    p_repair.set_defaults(func=_cmd_repair)

    p_brief = sub.add_parser("brief", help="assemble one morning brief and print it")
    p_brief.add_argument(
        "--post", action="store_true",
        help="also post it to the configured channels",
    )
    p_brief.set_defaults(func=_cmd_brief)

    p_run = sub.add_parser("run", help="start the always-on process")
    p_run.add_argument(
        "--no-checks", action="store_true",
        help="skip the fatal-checks doctor pass before starting",
    )
    p_run.set_defaults(func=_cmd_run)

    p_memory = sub.add_parser(
        "memory", help="see, correct, and teach the assistant's memory"
    )
    memory_sub = p_memory.add_subparsers(dest="memory_command")
    m_list = memory_sub.add_parser("list", help="list stored memories")
    m_list.add_argument("--query", default=None, help="search instead of listing all")
    m_list.set_defaults(func=_cmd_memory_list)
    m_forget = memory_sub.add_parser("forget", help="delete one memory by id")
    m_forget.add_argument("memory_id", help="an id (or unique prefix/suffix)")
    m_forget.add_argument("--yes", action="store_true", help="skip confirmation")
    m_forget.set_defaults(func=_cmd_memory_forget)
    m_remember = memory_sub.add_parser("remember", help="teach an explicit fact")
    m_remember.add_argument("text", help="the fact to remember")
    m_remember.set_defaults(func=_cmd_memory_remember)
    p_memory.set_defaults(func=_cmd_memory_help, parser=p_memory)

    p_importance = sub.add_parser(
        "importance", help="see and correct the learned per-sender importance profile"
    )
    importance_sub = p_importance.add_subparsers(dest="importance_command")
    i_list = importance_sub.add_parser("list", help="every sender's tier + reason")
    i_list.set_defaults(func=_cmd_importance_list)
    i_show = importance_sub.add_parser(
        "show", help="one sender's assessment + recorded signals"
    )
    i_show.add_argument("sender")
    i_show.set_defaults(func=_cmd_importance_show)
    i_pin = importance_sub.add_parser("pin", help="pin a sender to a tier")
    i_pin.add_argument("sender")
    i_pin.add_argument("tier", help="high, normal, or low")
    i_pin.set_defaults(func=_cmd_importance_pin)
    i_unpin = importance_sub.add_parser("unpin", help="remove a sender's pin")
    i_unpin.add_argument("sender")
    i_unpin.set_defaults(func=_cmd_importance_unpin)
    p_importance.set_defaults(func=_cmd_importance_help, parser=p_importance)

    p_autonomy = sub.add_parser(
        "autonomy", help="see and change the autonomy posture (grants are CLI-only)"
    )
    autonomy_sub = p_autonomy.add_subparsers(dest="autonomy_command")
    a_show = autonomy_sub.add_parser("show", help="current grants + suggestions")
    a_show.set_defaults(func=_cmd_autonomy_show)
    a_grant = autonomy_sub.add_parser(
        "grant",
        help="grant (action, domain) a rung, optionally scoped to a priority/tier",
    )
    a_grant.add_argument("action")
    a_grant.add_argument("domain")
    a_grant.add_argument("rung", help="e.g. propose, act_notify, or 3")
    a_grant.add_argument(
        "--priority", default=None,
        help="comma-separated scope: urgent,routine,noise (default: unscoped)",
    )
    a_grant.add_argument(
        "--tier", default=None,
        help="comma-separated scope: high,normal,low (default: unscoped)",
    )
    a_grant.set_defaults(func=_cmd_autonomy_grant)
    a_revoke = autonomy_sub.add_parser(
        "revoke",
        help="claw a grant back (every grant for the pair, or one scope with --priority/--tier)",
    )
    a_revoke.add_argument("action")
    a_revoke.add_argument("domain")
    a_revoke.add_argument(
        "--priority", default=None,
        help="revoke only the grant scoped to this priority set (comma-separated)",
    )
    a_revoke.add_argument(
        "--tier", default=None,
        help="revoke only the grant scoped to this tier set (comma-separated)",
    )
    a_revoke.set_defaults(func=_cmd_autonomy_revoke)
    a_record = autonomy_sub.add_parser(
        "record", help="track record of human decisions per (action, domain)"
    )
    a_record.add_argument("action", nargs="?", default=None)
    a_record.add_argument("domain", nargs="?", default=None)
    a_record.set_defaults(func=_cmd_autonomy_record)
    p_autonomy.set_defaults(func=_cmd_autonomy_help, parser=p_autonomy)

    return parser


def main(argv: list[str] | None = None) -> int:
    # `attune init` writes this file; every later CLI command must consume
    # it without requiring the operator to source secrets into their shell.
    from dotenv import load_dotenv

    load_dotenv()
    # Doctor reports this as an actionable row; other commands should not
    # repeat google-api-core's import-time warning wall.
    warnings.filterwarnings(
        "ignore",
        message=r"You are using a Python version .*",
        category=FutureWarning,
        module=r"google\.api_core\._python_version_support",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return int(args.func(args) or 0)


# --- subcommand dispatchers (lazy imports so --help needs nothing) ----------


def _cmd_init(args: Any) -> int:
    from .init_cmd import run_init

    return run_init(
        env_file=args.env_file,
        fresh=args.fresh,
        target=args.target,
        yes=args.yes,
    )


def _cmd_doctor(args: Any) -> int:
    from .doctor import run_doctor

    return run_doctor()


def _cmd_status(args: Any) -> int:
    from .setup_cmd import run_status

    return run_status(env_file=args.env_file, check=args.check)


def _cmd_repair(args: Any) -> int:
    from .setup_cmd import run_repair

    return run_repair(env_file=args.env_file, yes=args.yes)


def _cmd_brief(args: Any) -> int:
    from .brief_cmd import run_brief

    return run_brief(post=args.post)


def _cmd_run(args: Any) -> int:
    from .run_cmd import run_run

    return run_run(no_checks=args.no_checks)


def _cmd_memory_list(args: Any) -> int:
    from .memory_cmd import run_memory_list

    return run_memory_list(query=args.query)


def _cmd_memory_forget(args: Any) -> int:
    from .memory_cmd import run_memory_forget

    return run_memory_forget(args.memory_id, yes=args.yes)


def _cmd_memory_remember(args: Any) -> int:
    from .memory_cmd import run_memory_remember

    return run_memory_remember(args.text)


def _cmd_memory_help(args: Any) -> int:
    args.parser.print_help()
    return 1


def _cmd_importance_list(args: Any) -> int:
    from .importance_cmd import run_importance_list

    return run_importance_list()


def _cmd_importance_show(args: Any) -> int:
    from .importance_cmd import run_importance_show

    return run_importance_show(args.sender)


def _cmd_importance_pin(args: Any) -> int:
    from .importance_cmd import run_importance_pin

    return run_importance_pin(args.sender, args.tier)


def _cmd_importance_unpin(args: Any) -> int:
    from .importance_cmd import run_importance_unpin

    return run_importance_unpin(args.sender)


def _cmd_importance_help(args: Any) -> int:
    args.parser.print_help()
    return 1


def _cmd_autonomy_show(args: Any) -> int:
    from .autonomy_cmd import run_autonomy_show

    return run_autonomy_show()


def _cmd_autonomy_grant(args: Any) -> int:
    from .autonomy_cmd import run_autonomy_grant

    return run_autonomy_grant(
        args.action, args.domain, args.rung,
        priority=args.priority, tier=args.tier,
    )


def _cmd_autonomy_revoke(args: Any) -> int:
    from .autonomy_cmd import run_autonomy_revoke

    return run_autonomy_revoke(
        args.action, args.domain, priority=args.priority, tier=args.tier,
    )


def _cmd_autonomy_record(args: Any) -> int:
    from .autonomy_cmd import run_autonomy_record

    return run_autonomy_record(args.action, args.domain)


def _cmd_autonomy_help(args: Any) -> int:
    args.parser.print_help()
    return 1
