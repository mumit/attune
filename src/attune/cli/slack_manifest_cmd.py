"""``attune slack manifest``: a ready-to-paste Slack app manifest (UX review
persona A item #8, G20).

Covers exactly what ``docs/install/slack-app.md``'s self-hosted section
configures by hand: Socket Mode, the four bot token scopes, the
``message.im`` event subscription, App Home's Messages tab, and
Interactivity. Three steps stay manual because Slack's manifest format has
no field for them: creating the app from this manifest, generating the
app-level token (``connections:write`` scope) and installing the app for the
bot token, and copying the operator's own member ID.
"""

from __future__ import annotations

import json
from typing import Callable

APP_NAME_DEFAULT = "Attune"

# docs/install/slack-app.md, self-hosted manual path step 4.
BOT_SCOPES: tuple[str, ...] = ("chat:write", "im:history", "im:read", "im:write")

# Manual path step 5 — the only bot event Attune's Socket Mode client consumes.
BOT_EVENTS: tuple[str, ...] = ("message.im",)


def build_manifest(*, app_name: str = APP_NAME_DEFAULT) -> dict:
    """Return a Slack app manifest (the schema accepted by
    https://api.slack.com/apps -> Create New App -> From an app manifest)."""
    return {
        "display_information": {"name": app_name},
        "features": {
            "bot_user": {"display_name": app_name, "always_online": True},
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
        },
        "oauth_config": {
            "scopes": {"bot": list(BOT_SCOPES)},
        },
        "settings": {
            "event_subscriptions": {"bot_events": list(BOT_EVENTS)},
            "interactivity": {"is_enabled": True},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def run_slack_manifest(
    *, app_name: str = APP_NAME_DEFAULT, out: Callable[[str], None] = print
) -> int:
    manifest = build_manifest(app_name=app_name)
    out(json.dumps(manifest, indent=2))
    out("")
    out(
        "Paste the JSON above at https://api.slack.com/apps -> Create New "
        "App -> From an app manifest (the create page accepts JSON directly)."
    )
    out("Remaining manual steps (Slack manifests cannot automate these):")
    out("  1. Create the app from this manifest, selecting your workspace.")
    out(
        "  2. Basic Information -> App-Level Tokens -> generate a token "
        "with the connections:write scope; save it as SLACK_APP_TOKEN."
    )
    out(
        "  3. Install (or reinstall) the app, then save the Bot User OAuth "
        "Token (xoxb-...) as SLACK_BOT_TOKEN."
    )
    out(
        "  4. In Slack, open your profile -> More -> Copy member ID; put "
        "the U... value in ATTUNE_SLACK_CHANNEL and ATTUNE_SLACK_ALLOWED_USERS."
    )
    return 0
