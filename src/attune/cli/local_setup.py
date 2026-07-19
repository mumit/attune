"""Deterministic local provisioning for ``attune init --target local``."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.resources import files
import json
import os
import shlex
import shutil
import subprocess
from typing import Callable, Sequence


Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class LocalPlan:
    plan_id: str
    command: tuple[str, ...]
    resources: tuple[str, ...]
    summary: tuple[str, ...]

    @property
    def digest(self) -> str:
        payload = json.dumps(
            {
                "plan_id": self.plan_id,
                "command": self.command,
                "resources": self.resources,
                "summary": self.summary,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LocalProvisionError(RuntimeError):
    """A deterministic local provisioning operation failed."""


def compose_file() -> str:
    return os.fspath(files("attune").joinpath("resources/local-compose.yml"))


def build_local_plan(*, compose_path: str | None = None) -> LocalPlan:
    path = os.path.abspath(compose_path or compose_file())
    return LocalPlan(
        plan_id="local-qdrant-v1",
        command=(
            "docker",
            "compose",
            "--project-name",
            "attune",
            "--file",
            path,
            "up",
            "--detach",
        ),
        resources=(
            "docker-compose-project:attune",
            "service:qdrant",
            "volume:qdrant_data",
        ),
        summary=(
            "Start Qdrant 1.18.2 in the Docker Compose project 'attune'.",
            "Bind its HTTP API only to 127.0.0.1:6333.",
            "Persist vectors in the Docker volume 'attune_qdrant_data'.",
            "Do not pass .env or any Attune credential to the container.",
        ),
    )


def render_local_plan(plan: LocalPlan) -> list[str]:
    return [
        f"Local deployment plan ({plan.plan_id}):",
        *(f"  - {line}" for line in plan.summary),
        "  Command: " + shlex.join(plan.command),
    ]


#: Non-ATTUNE-prefixed variables that still carry Attune-held credentials —
#: scrubbed from child processes alongside every ``ATTUNE_*`` value.
_SECRET_ENV_NAMES = frozenset({"SLACK_APP_TOKEN", "SLACK_BOT_TOKEN"})


def scrubbed_subprocess_env() -> dict[str, str]:
    """The environment external tool subprocesses (docker compose, gcloud)
    receive: the parent environment minus every ``ATTUNE_``-prefixed value
    and known credential-bearing names. The tools need their own config
    (PATH, HOME, DOCKER_*/CLOUDSDK_*) to work, so this is a denylist of what
    Attune holds, not an allowlist — the decision log's "receives no Attune
    environment or credential" made literal rather than aspirational."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ATTUNE_") and key not in _SECRET_ENV_NAMES
    }


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        shell=False,
        env=scrubbed_subprocess_env(),
    )


def apply_local_plan(
    plan: LocalPlan, *, runner: Runner | None = None
) -> subprocess.CompletedProcess[str]:
    if runner is None and shutil.which("docker") is None:
        raise LocalProvisionError(
            "Docker is not installed or is not on PATH; install Docker and rerun "
            "attune init --target local"
        )
    result = (runner or _default_runner)(plan.command)
    if result.returncode:
        detail = (result.stderr or result.stdout or "Docker Compose failed").strip()
        raise LocalProvisionError(detail)
    return result
