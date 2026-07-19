"""Versioned, secret-free state for resumable setup workflows.

Setup state records what Attune attempted and what it verified.  It never stores
configuration values or credentials; the environment file and secret stores
remain the source of truth for those values.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
import stat
import tempfile
from typing import Any


SCHEMA_VERSION = 1
STEP_NAMES = ("configure", "apply", "validate")
STEP_STATUSES = frozenset(
    {"not_started", "in_progress", "succeeded", "failed", "declined", "skipped"}
)


class SetupStateError(ValueError):
    """A setup state file is malformed or belongs to another workflow."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StepState:
    status: str = "not_started"
    updated_at: str = field(default_factory=_now)
    detail: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> "StepState":
        if not isinstance(value, dict):
            raise SetupStateError("setup step must be an object")
        status = value.get("status", "not_started")
        if status not in STEP_STATUSES:
            raise SetupStateError(f"invalid setup step status: {status!r}")
        return cls(
            status=status,
            updated_at=str(value.get("updated_at") or _now()),
            detail=str(value.get("detail") or ""),
        )


@dataclass
class SetupState:
    target: str
    env_file: str
    data_dir: str
    schema_version: int = SCHEMA_VERSION
    config_digest: str = ""
    plan_digest: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    resources: list[str] = field(default_factory=list)
    steps: dict[str, StepState] = field(
        default_factory=lambda: {name: StepState() for name in STEP_NAMES}
    )

    @classmethod
    def load_or_create(
        cls, path: str, *, target: str, env_file: str, data_dir: str
    ) -> "SetupState":
        env_file = os.path.abspath(env_file)
        data_dir = os.path.abspath(os.path.expanduser(data_dir))
        if not os.path.exists(path):
            return cls(target=target, env_file=env_file, data_dir=data_dir)
        if os.path.islink(path):
            raise SetupStateError("setup state must not be a symbolic link")
        state_stat = os.stat(path)
        if not stat.S_ISREG(state_stat.st_mode):
            raise SetupStateError("setup state must be a regular file")
        if os.name == "posix" and state_stat.st_mode & 0o077:
            raise SetupStateError("setup state must have owner-only permissions (0600)")
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as exc:
            raise SetupStateError(f"cannot read setup state: {exc}") from exc
        if not isinstance(raw, dict):
            raise SetupStateError("setup state must be an object")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise SetupStateError(
                f"unsupported setup schema {raw.get('schema_version')!r}; "
                f"expected {SCHEMA_VERSION}"
            )
        if raw.get("target") != target:
            raise SetupStateError(
                f"setup state belongs to target {raw.get('target')!r}, not {target!r}"
            )
        if os.path.abspath(str(raw.get("env_file") or "")) != env_file:
            raise SetupStateError("setup state belongs to a different environment file")
        if os.path.abspath(str(raw.get("data_dir") or "")) != data_dir:
            raise SetupStateError("setup state belongs to a different data directory")
        raw_steps = raw.get("steps") or {}
        if not isinstance(raw_steps, dict):
            raise SetupStateError("setup steps must be an object")
        state = cls(
            target=target,
            env_file=env_file,
            data_dir=data_dir,
            config_digest=str(raw.get("config_digest") or ""),
            plan_digest=str(raw.get("plan_digest") or ""),
            created_at=str(raw.get("created_at") or _now()),
            updated_at=str(raw.get("updated_at") or _now()),
            resources=[str(item) for item in raw.get("resources") or []],
            steps={
                name: StepState.from_dict(raw_steps.get(name, {}))
                for name in STEP_NAMES
            },
        )
        return state

    def record_configuration(self, digest: str) -> None:
        if self.config_digest and self.config_digest != digest:
            self.steps["apply"] = StepState()
            self.steps["validate"] = StepState()
            self.resources = []
        self.config_digest = digest
        self.set_step("configure", "succeeded", "environment written with mode 0600")

    def record_plan(self, digest: str) -> None:
        if self.plan_digest and self.plan_digest != digest:
            self.steps["apply"] = StepState()
            self.steps["validate"] = StepState()
            self.resources = []
        self.plan_digest = digest
        self.updated_at = _now()

    def set_step(self, name: str, status: str, detail: str = "") -> None:
        if name not in STEP_NAMES:
            raise SetupStateError(f"unknown setup step: {name!r}")
        if status not in STEP_STATUSES:
            raise SetupStateError(f"invalid setup step status: {status!r}")
        now = _now()
        self.steps[name] = StepState(status=status, updated_at=now, detail=detail)
        self.updated_at = now

    def save(self, path: str) -> None:
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        payload = asdict(self)
        fd, temporary = tempfile.mkstemp(
            prefix=".attune-setup-", suffix=".json", dir=directory, text=True
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


def setup_state_path(data_dir: str) -> str:
    return os.path.join(os.path.abspath(os.path.expanduser(data_dir)), "setup-state.json")
