"""``attune routine`` — user-authored recurring proactivity (build prompt 32,
task 1). See ``orchestrator/routines.py`` for the validation/storage model
this CLI is a thin wrapper over.
"""

from __future__ import annotations

from typing import Any, Callable


def _resolve_store(settings: Any) -> tuple[Any, Any]:
    from ..config import Settings
    from ..orchestrator.routines import open_routine_store

    resolved_settings = settings or Settings.from_env()
    store = open_routine_store(
        resolved_settings.routine_state_path, brief_time=resolved_settings.brief_time,
    )
    return resolved_settings, store


def _slugify(request: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "_", request.strip().lower()).strip("_")
    return (slug or "routine")[:40]


def _unique_name(store: Any, base: str) -> str:
    if store.get(base) is None:
        return base
    for i in range(2, 1000):
        candidate = f"{base}_{i}"
        if store.get(candidate) is None:
            return candidate
    raise RuntimeError("could not derive a unique routine name")  # pragma: no cover


def run_routine_add(
    request: str,
    *,
    schedule: str,
    name: str | None = None,
    settings: Any = None,
    client: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    from ..llm import make_client
    from ..orchestrator.routines import Routine, RoutineError, parse_schedule, validate_routine_request

    resolved_settings, store = _resolve_store(settings)
    if name is not None and store.get(name) is not None:
        out(f"A routine named {name!r} already exists — remove it first or choose another name.")
        return 2
    resolved_name = name or _unique_name(store, _slugify(request))

    try:
        parse_schedule(schedule)
    except RoutineError as exc:
        out(f"Cannot add routine: {exc}")
        return 2

    resolved_client = client or make_client(settings=resolved_settings)
    try:
        plan = validate_routine_request(
            resolved_client, request, timezone_name=resolved_settings.timezone,
        )
    except RoutineError as exc:
        out(f"Cannot add routine: {exc}")
        return 2

    from datetime import datetime, timezone

    store.add(Routine(
        name=resolved_name, request=request, schedule=schedule,
        created_at=datetime.now(timezone.utc),
    ))
    out(f"Added routine {resolved_name!r} ({plan.intent.value}): {schedule!r} — {request!r}")
    return 0


def run_routine_list(
    *, settings: Any = None, out: Callable[[str], None] = print,
) -> int:
    _, store = _resolve_store(settings)
    routines = store.list()
    if not routines:
        out("No routines configured.")
        return 0
    for routine in sorted(routines, key=lambda r: r.name):
        out(f"{routine.name:<20} {routine.schedule:<16} {routine.request}")
    return 0


def run_routine_show(
    name: str, *, settings: Any = None, out: Callable[[str], None] = print,
) -> int:
    _, store = _resolve_store(settings)
    routine = store.get(name)
    if routine is None:
        out(f"No such routine: {name}")
        return 1
    out(f"name: {routine.name}")
    out(f"schedule: {routine.schedule}")
    out(f"request: {routine.request}")
    out(f"created_at: {routine.created_at.isoformat()}")
    return 0


def run_routine_remove(
    name: str, *, settings: Any = None, out: Callable[[str], None] = print,
) -> int:
    _, store = _resolve_store(settings)
    if not store.remove(name):
        out(f"No such routine: {name}")
        return 1
    out(f"Removed routine: {name}")
    return 0


def run_routine_run(
    name: str,
    *,
    runtime_factory: Callable[[], Any] | None = None,
    settings: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    """One-off preview: fire ``name`` right now and print what it produces,
    without waiting for its scheduled time — the exact same execution path
    ``runtime.Runtime.run_scheduled_routine`` uses (never a second
    implementation)."""
    _, store = _resolve_store(settings)
    routine = store.get(name)
    if routine is None:
        out(f"No such routine: {name}")
        return 1

    from ..runtime import build_runtime

    rt = (runtime_factory or build_runtime)()
    result = rt.run_scheduled_routine(routine)
    text = result.summary if hasattr(result, "summary") else str(result)
    out(text)
    return 0
