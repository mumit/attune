"""Tests for credentials.py — no live Google connection required.

google.auth and google.oauth2 are mocked with simple fakes so the module
can be exercised without installing the google extras.
"""

from __future__ import annotations

import json
import sys
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fake google.auth / google.oauth2 modules injected into sys.modules
# ---------------------------------------------------------------------------

class _FakeSACreds:
    """Returned by service_account.Credentials.from_service_account_info."""
    def __init__(self, info, *, scopes):
        self.info = info
        self.scopes = scopes


class _FakeUserCreds:
    """Returned by credentials.Credentials.from_authorized_user_info."""
    def __init__(self, info):
        self.info = info


class _FakeADCCreds:
    """Returned by google.auth.default."""
    pass


def _build_google_mocks():
    """Return (google_mod, auth_mod, sa_mod, user_creds_mod) mocks."""
    google_mod = ModuleType("google")
    auth_mod = ModuleType("google.auth")
    google_mod.auth = auth_mod

    attune_creds = _FakeADCCreds()
    auth_mod.default = MagicMock(return_value=(attune_creds, "project"))

    sa_mod = ModuleType("google.oauth2.service_account")
    sa_cls = MagicMock(side_effect=lambda info, *, scopes: _FakeSACreds(info, scopes=scopes))
    sa_mod.Credentials = MagicMock()
    sa_mod.Credentials.from_service_account_info = sa_cls

    user_creds_mod = ModuleType("google.oauth2.credentials")
    user_cls = MagicMock(side_effect=lambda info: _FakeUserCreds(info))
    user_creds_mod.Credentials = MagicMock()
    user_creds_mod.Credentials.from_authorized_user_info = user_cls

    google_oauth2_mod = ModuleType("google.oauth2")
    google_oauth2_mod.service_account = sa_mod
    google_oauth2_mod.credentials = user_creds_mod
    google_mod.oauth2 = google_oauth2_mod

    return google_mod, auth_mod, sa_mod, user_creds_mod, attune_creds


@pytest.fixture()
def google_mocks(tmp_path, monkeypatch):
    """Inject fake google modules and yield (sa_mod, user_creds_mod, attune_creds, tmp_path)."""
    google_mod, auth_mod, sa_mod, user_creds_mod, attune_creds = _build_google_mocks()

    patches = {
        "google": google_mod,
        "google.auth": auth_mod,
        "google.oauth2": google_mod.oauth2,
        "google.oauth2.service_account": sa_mod,
        "google.oauth2.credentials": user_creds_mod,
    }
    with patch.dict(sys.modules, patches):
        # Force reimport so the lazy imports pick up our fakes.
        if "attune.credentials" in sys.modules:
            del sys.modules["attune.credentials"]
        yield sa_mod, user_creds_mod, attune_creds, tmp_path


# ---------------------------------------------------------------------------
# Service-account credentials
# ---------------------------------------------------------------------------

def test_service_account_creds_loaded(google_mocks):
    sa_mod, _, _, tmp_path = google_mocks
    cred_file = tmp_path / "sa.json"
    sa_info = {"type": "service_account", "project_id": "test"}
    cred_file.write_text(json.dumps(sa_info))

    from attune.credentials import SCOPES_DEFAULT, load_google_credentials
    from attune.config import Settings

    settings = Settings.from_env(
        {"ATTUNE_WORKSPACE_BACKEND": "mcp",
         "ATTUNE_MEM0_URL": "", "ATTUNE_AUDIT_LOG_PATH": "",
         "ATTUNE_GOOGLE_CREDENTIALS_FILE": str(cred_file)}
    )
    creds = load_google_credentials(settings)

    assert isinstance(creds, _FakeSACreds)
    assert creds.info == sa_info
    assert list(SCOPES_DEFAULT) == list(creds.scopes)


def test_service_account_custom_scopes(google_mocks):
    sa_mod, _, _, tmp_path = google_mocks
    cred_file = tmp_path / "sa.json"
    sa_info = {"type": "service_account"}
    cred_file.write_text(json.dumps(sa_info))

    from attune.credentials import load_google_credentials
    from attune.config import Settings

    settings = Settings.from_env(
        {"ATTUNE_WORKSPACE_BACKEND": "mcp",
         "ATTUNE_MEM0_URL": "", "ATTUNE_AUDIT_LOG_PATH": "",
         "ATTUNE_GOOGLE_CREDENTIALS_FILE": str(cred_file)}
    )
    custom = ["https://www.googleapis.com/auth/gmail.readonly"]
    creds = load_google_credentials(settings, scopes=custom)
    assert list(creds.scopes) == custom


# ---------------------------------------------------------------------------
# OAuth user credentials
# ---------------------------------------------------------------------------

def test_oauth_user_creds_loaded(google_mocks):
    _, user_creds_mod, _, tmp_path = google_mocks
    cred_file = tmp_path / "user.json"
    user_info = {"type": "authorized_user", "client_id": "c", "client_secret": "s", "refresh_token": "r"}
    cred_file.write_text(json.dumps(user_info))

    from attune.credentials import load_google_credentials
    from attune.config import Settings

    settings = Settings.from_env(
        {"ATTUNE_WORKSPACE_BACKEND": "mcp",
         "ATTUNE_MEM0_URL": "", "ATTUNE_AUDIT_LOG_PATH": "",
         "ATTUNE_GOOGLE_CREDENTIALS_FILE": str(cred_file)}
    )
    creds = load_google_credentials(settings)
    assert isinstance(creds, _FakeUserCreds)
    assert creds.info == user_info


# ---------------------------------------------------------------------------
# ADC fallback
# ---------------------------------------------------------------------------

def test_attune_fallback_used_when_no_file(google_mocks):
    _, _, attune_creds, _ = google_mocks
    from attune.credentials import load_google_credentials, SCOPES_DEFAULT
    from attune.config import Settings

    settings = Settings.from_env(
        {"ATTUNE_WORKSPACE_BACKEND": "mcp",
         "ATTUNE_MEM0_URL": "", "ATTUNE_AUDIT_LOG_PATH": ""}
        # no ATTUNE_GOOGLE_CREDENTIALS_FILE
    )
    creds = load_google_credentials(settings)
    assert creds is attune_creds


def test_attune_receives_default_scopes(google_mocks):
    _, _, _, _ = google_mocks
    import google.auth as _auth
    from attune.credentials import load_google_credentials, SCOPES_DEFAULT
    from attune.config import Settings

    settings = Settings.from_env(
        {"ATTUNE_WORKSPACE_BACKEND": "mcp",
         "ATTUNE_MEM0_URL": "", "ATTUNE_AUDIT_LOG_PATH": ""}
    )
    load_google_credentials(settings)
    call_scopes = _auth.default.call_args[1]["scopes"]
    assert call_scopes == list(SCOPES_DEFAULT)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_missing_credentials_file_raises(google_mocks):
    _, _, _, tmp_path = google_mocks
    from attune.credentials import load_google_credentials
    from attune.config import Settings

    settings = Settings.from_env(
        {"ATTUNE_WORKSPACE_BACKEND": "mcp",
         "ATTUNE_MEM0_URL": "", "ATTUNE_AUDIT_LOG_PATH": "",
         "ATTUNE_GOOGLE_CREDENTIALS_FILE": str(tmp_path / "nonexistent.json")}
    )
    with pytest.raises(FileNotFoundError):
        load_google_credentials(settings)
