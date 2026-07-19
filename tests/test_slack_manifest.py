"""Golden test for ``attune slack manifest`` (UX review persona A item #8,
G20): the printed manifest must cover exactly what docs/getting-started.md
section 6 configures by hand."""

from __future__ import annotations

import json

from attune.cli.slack_manifest_cmd import BOT_EVENTS, BOT_SCOPES, build_manifest, run_slack_manifest


def test_manifest_declares_required_bot_scopes():
    manifest = build_manifest()
    assert manifest["oauth_config"]["scopes"]["bot"] == list(BOT_SCOPES)
    assert set(BOT_SCOPES) == {"chat:write", "im:history", "im:read", "im:write"}


def test_manifest_declares_message_im_event_subscription():
    manifest = build_manifest()
    assert manifest["settings"]["event_subscriptions"]["bot_events"] == list(BOT_EVENTS)
    assert BOT_EVENTS == ("message.im",)


def test_manifest_enables_socket_mode_and_interactivity():
    manifest = build_manifest()
    assert manifest["settings"]["socket_mode_enabled"] is True
    assert manifest["settings"]["interactivity"]["is_enabled"] is True


def test_manifest_enables_app_home_messages_tab():
    manifest = build_manifest()
    assert manifest["features"]["app_home"]["messages_tab_enabled"] is True


def test_manifest_uses_custom_app_name():
    manifest = build_manifest(app_name="MyBot")
    assert manifest["display_information"]["name"] == "MyBot"
    assert manifest["features"]["bot_user"]["display_name"] == "MyBot"


def test_manifest_is_valid_json():
    manifest = build_manifest()
    round_tripped = json.loads(json.dumps(manifest))
    assert round_tripped == manifest


def test_run_slack_manifest_prints_manifest_and_manual_step_instructions():
    lines = []
    code = run_slack_manifest(out=lines.append)
    rendered = "\n".join(lines)

    assert code == 0
    assert '"socket_mode_enabled": true' in rendered
    assert "connections:write" in rendered
    assert "SLACK_APP_TOKEN" in rendered
    assert "xoxb-" in rendered
    assert "SLACK_BOT_TOKEN" in rendered
    assert "member ID" in rendered
    assert "ATTUNE_SLACK_ALLOWED_USERS" in rendered


def test_cli_parser_registers_slack_manifest_subcommand():
    from attune.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["slack", "manifest"])
    assert hasattr(args, "func")


def test_cli_slack_without_subcommand_prints_help(capsys):
    from attune.cli import main

    assert main(["slack"]) == 1
    assert "manifest" in capsys.readouterr().out
