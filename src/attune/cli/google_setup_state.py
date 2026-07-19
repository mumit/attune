"""Versioned, secret-free state for the guided Google Cloud setup checklist
(``attune init --google-setup``; UX item #1, G20).

Mirrors ``setup_state.py``'s discipline exactly (schema-versioned JSON,
atomic writes, owner-only permissions, no symlinks, no configuration values
or credentials ever recorded) but tracks a different thing: which checklist
steps the operator has confirmed or skipped, and whether the OAuth consent
screen ended up Internal or External+Testing. That single field feeds
Doctor's ``google-oauth-app`` check and ``attune init``'s completion
reminder — see ``docs/decisions.md``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
import stat
import tempfile

from .setup_state import STEP_STATUSES, SetupStateError, StepState

SCHEMA_VERSION = 1

# Numbered 1-7 in checklist output; order matches the real Google Cloud
# Console flow (project -> APIs -> consent screen branding -> audience ->
# scopes -> OAuth client) and docs/getting-started.md section 4A.
STEP_IDS = (
    "create_project",
    "enable_gmail_api",
    "enable_calendar_api",
    "consent_branding",
    "consent_audience",
    "consent_scopes",
    "oauth_client",
)

CONSENT_MODES = frozenset({"", "internal", "external_testing", "external_published"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class GoogleSetupState:
    schema_version: int = SCHEMA_VERSION
    consent_mode: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    steps: dict[str, StepState] = field(
        default_factory=lambda: {step_id: StepState() for step_id in STEP_IDS}
    )

    @classmethod
    def load_or_create(cls, path: str) -> "GoogleSetupState":
        if not os.path.exists(path):
            return cls()
        if os.path.islink(path):
            raise SetupStateError("google setup state must not be a symbolic link")
        state_stat = os.stat(path)
        if not stat.S_ISREG(state_stat.st_mode):
            raise SetupStateError("google setup state must be a regular file")
        if os.name == "posix" and state_stat.st_mode & 0o077:
            raise SetupStateError(
                "google setup state must have owner-only permissions (0600)"
            )
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            raise SetupStateError(f"cannot read google setup state: {exc}") from exc
        if not isinstance(raw, dict):
            raise SetupStateError("google setup state must be an object")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise SetupStateError(
                f"unsupported google setup schema {raw.get('schema_version')!r}; "
                f"expected {SCHEMA_VERSION}"
            )
        consent_mode = str(raw.get("consent_mode") or "")
        if consent_mode not in CONSENT_MODES:
            raise SetupStateError(f"invalid consent mode: {consent_mode!r}")
        raw_steps = raw.get("steps") or {}
        if not isinstance(raw_steps, dict):
            raise SetupStateError("google setup steps must be an object")
        return cls(
            consent_mode=consent_mode,
            created_at=str(raw.get("created_at") or _now()),
            updated_at=str(raw.get("updated_at") or _now()),
            steps={
                step_id: StepState.from_dict(raw_steps.get(step_id, {}))
                for step_id in STEP_IDS
            },
        )

    def set_step(self, step_id: str, status: str, detail: str = "") -> None:
        if step_id not in STEP_IDS:
            raise SetupStateError(f"unknown google setup step: {step_id!r}")
        if status not in STEP_STATUSES:
            raise SetupStateError(f"invalid setup step status: {status!r}")
        now = _now()
        self.steps[step_id] = StepState(status=status, updated_at=now, detail=detail)
        self.updated_at = now

    def set_consent_mode(self, mode: str) -> None:
        if mode not in CONSENT_MODES:
            raise SetupStateError(f"invalid consent mode: {mode!r}")
        self.consent_mode = mode
        self.updated_at = _now()

    def save(self, path: str) -> None:
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        payload = asdict(self)
        fd, temporary = tempfile.mkstemp(
            prefix=".attune-google-setup-", suffix=".json", dir=directory, text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def google_setup_state_path(data_dir: str) -> str:
    return os.path.join(
        os.path.abspath(os.path.expanduser(data_dir)), "google-setup-state.json"
    )
