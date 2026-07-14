"""``attune run`` — start the always-on process, doctor-gated
(roadmap prompt 08).

A fast pass of the fatal doctor checks (environment, data directory, model API,
workspace backend) runs first so a misconfigured deployment fails in two
seconds with a fix hint instead of half-starting and dying in a pull loop.
``--no-checks`` skips the gate.
"""

from __future__ import annotations

import os
from typing import Any, Callable


def run_run(
    *,
    no_checks: bool = False,
    runtime_factory: Callable[[], Any] | None = None,
    doctor: Callable[..., int] | None = None,
    out: Callable[[str], None] = print,
) -> int:
    if not no_checks:
        from .doctor import run_doctor

        code = (doctor or run_doctor)(fatal_only=True)
        if code != 0:
            out("Fatal checks failed — fix the above (or start with --no-checks).")
            return code

    from ..logging_setup import configure

    configure(
        level=os.environ.get("ATTUNE_LOG_LEVEL", "INFO"),
        json_mode=os.environ.get("ATTUNE_LOG_JSON", "") == "1",
    )

    from ..runtime import build_runtime

    (runtime_factory or build_runtime)().run()
    return 0
