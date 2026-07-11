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
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
)


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
            "`pip install google-auth` (or `pip install aidedecamp[google]`)."
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
