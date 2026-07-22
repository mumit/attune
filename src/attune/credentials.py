"""Load Google OAuth credentials for API access (design doc 4.3, 4.6).

Resolution order:
1. ``settings.google_credentials_file`` → detect credential type from the JSON
   ``"type"`` field and construct accordingly (service account or OAuth user).
2. Application Default Credentials (``google.auth.default``) — works with the
   ``GOOGLE_APPLICATION_CREDENTIALS`` env var, ``gcloud auth application-default
   login``, or the Compute Engine / GKE metadata server.

google-auth is imported lazily so the package loads without it; install the
``[google]`` extra to use this module.
"""

from __future__ import annotations

from typing import Any

from .config import Settings

# Minimum scope set for read + draft. Extend only when an autonomy grant
# explicitly authorises a wider action (design rule 4 / scope discipline).
SCOPES_DEFAULT: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    # Approved conflict holds call events.insert; the permission matrix still
    # keeps that write at PROPOSE until the operator explicitly grants more.
    "https://www.googleapis.com/auth/calendar.events",
)

# Optional user-auth scopes for Chat polling and Workspace Events. Proactive
# Cards v2 use a separate app-auth credential and chat.bot (not wired yet).
SCOPES_CHAT: tuple[str, ...] = (
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
)

SCOPE_CHAT_BOT = "https://www.googleapis.com/auth/chat.bot"


def load_google_credentials(
    settings: Settings | None = None,
    scopes: tuple[str, ...] | list[str] | None = None,
) -> Any:
    """Return a ``google.auth`` credentials object for the configured deployment.

    Args:
        settings: if None, loads from environment via ``Settings.from_env()``.
        scopes: OAuth scopes to request. Defaults to :data:`SCOPES_DEFAULT`.
            Ignored when the credentials file already contains a token with
            its own scope set (OAuth user credentials); required for service
            accounts.

    Raises:
        ImportError: if ``google-auth`` is not installed.
        FileNotFoundError: if ``settings.google_credentials_file`` is set but
            the file does not exist.
    """
    try:
        import google.auth
        from google.oauth2 import credentials as _user_creds
        from google.oauth2 import service_account as _sa
    except ImportError as exc:
        raise ImportError(
            "load_google_credentials requires google-auth. "
            "`pip install google-auth` (or `pip install attune[google]`)."
        ) from exc

    resolved = list(scopes or SCOPES_DEFAULT)
    settings = settings or Settings.from_env()
    cred_file = settings.google_credentials_file

    if cred_file:
        import json

        with open(cred_file) as fh:
            info = json.load(fh)

        if info.get("type") == "service_account":
            return _sa.Credentials.from_service_account_info(
                info, scopes=resolved
            )
        # OAuth 2.0 user credentials (from gcloud or the OAuth consent screen).
        return _user_creds.Credentials.from_authorized_user_info(info)

    creds, _ = google.auth.default(scopes=resolved)
    return creds


def load_google_chat_credentials(settings: Settings | None = None) -> Any:
    """Load the distinct app identity used to send/reply as the Chat app.

    Design rule 4 (credentials have narrow roles): this must be a credential
    dedicated to the Chat app, never the principal's Gmail/Calendar OAuth
    grant reused. That separation does not require any one *mechanism* —
    Google's Chat API documents two supported ways for an app to
    authenticate (see "Authentication types for Google Chat API" at
    developers.google.com/workspace/chat/authenticate-authorize):

    - **App authentication** — a service account, optionally with
      domain-wide delegation. This is the original, still-recommended path
      here, requested with :data:`SCOPE_CHAT_BOT`.
    - **User authentication** — an OAuth *user* credential (the on-disk
      shape produced by the same ``google-auth-oauthlib`` flow already used
      for Gmail/Calendar, ``type: authorized_user``), requested with
      :data:`SCOPES_CHAT` instead of the app-only ``chat.bot`` scope. This
      is the alternative for operators whose organization does not permit
      creating IAM service-account keys — it is still a second, dedicated
      credential file, just obtained by an OAuth consent flow instead of a
      downloaded key.

    Whichever mechanism is used, the file must be distinct from
    ``ATTUNE_GOOGLE_CREDENTIALS_FILE``; ``attune doctor`` refuses to start if
    the two paths are identical. This project has not exercised the
    user-authentication path against a live Chat app with interactive Cards
    — verify current behavior (in particular, whether button-click routing
    still reaches the app's configured interaction endpoint) against
    Google's own documentation before relying on it for approval cards, per
    ``docs/deployment.md``'s Google Chat section.
    """
    settings = settings or Settings.from_env()
    path = settings.chat_credentials_file
    if not path:
        raise ValueError(
            "Google Chat requires ATTUNE_CHAT_CREDENTIALS_FILE pointing to a "
            "service-account or OAuth user-credential JSON"
        )
    try:
        import json
        from google.oauth2 import credentials as _user_creds
        from google.oauth2 import service_account
    except ImportError as exc:
        raise ImportError("Google Chat requires `pip install attune[google]`") from exc
    with open(path) as fh:
        info = json.load(fh)
    kind = info.get("type")
    if kind == "service_account":
        return service_account.Credentials.from_service_account_info(
            info, scopes=[SCOPE_CHAT_BOT]
        )
    if kind == "authorized_user":
        # Scopes are already embedded in a stored user-credential token
        # (same convention as load_google_credentials' OAuth-user branch);
        # SCOPES_CHAT documents what must have been granted at consent time.
        return _user_creds.Credentials.from_authorized_user_info(info)
    raise ValueError(
        "ATTUNE_CHAT_CREDENTIALS_FILE must contain a service account "
        '(type: "service_account") or an OAuth user credential '
        '(type: "authorized_user"), not: ' + repr(kind)
    )
