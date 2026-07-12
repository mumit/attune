"""``aidedecamp autonomy`` — see and change the autonomy posture, and see
the track record that earns graduations (roadmap prompt 12).

This is deliberately the ONLY surface that can grant or revoke (rule 3):
a chat channel that relays untrusted content must never be able to escalate
autonomy, so chat gets show-and-suggest only. Parsing is strict — a typo
errors, never silently defaults.
"""

from __future__ import annotations

from typing import Any, Callable


def run_autonomy_show(
    *,
    settings: Any = None,
    audit_log: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    from ..orchestrator import show_matrix, suggest_graduations

    settings, store, matrix, audit = _resolve(settings, audit_log)
    out(show_matrix(matrix))
    suggestions = suggest_graduations(audit, matrix) if audit is not None else []
    if suggestions:
        out("")
        out("Graduation suggestions (a human always makes the grant):")
        for s in suggestions:
            out(f"  - {s.render()}")
    return 0


def run_autonomy_grant(
    action: str,
    domain: str,
    rung: str,
    *,
    settings: Any = None,
    audit_log: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    from ..orchestrator import grant
    from ..orchestrator.autonomy import Action
    from ..orchestrator.grants import parse_action, parse_domain, parse_rung

    try:
        parsed = (parse_action(action), parse_domain(domain), parse_rung(rung))
    except (ValueError, KeyError):
        out(f"Unknown action/domain/rung: {action} {domain} {rung}")
        _print_vocabulary(out)
        return 2

    settings, store, matrix, audit = _resolve(settings, audit_log)
    new_matrix = grant(
        store, matrix, *parsed, audit_log=audit, user_id=settings.user_id
    )
    out(
        f"Granted: {parsed[0].value} on {parsed[1].value} at "
        f"{parsed[2].name} (persisted to {settings.autonomy_state_path})"
    )
    if parsed[0] == Action.SEND_REPLY:
        out(
            "⚠ Note: sending remains structurally disabled regardless of this "
            "grant — it additionally requires send_enabled and a real "
            "gmail.send scope (rule 4), each a deliberate, separately-reviewed "
            "change."
        )
    return 0


def run_autonomy_revoke(
    action: str,
    domain: str,
    *,
    settings: Any = None,
    audit_log: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    from ..orchestrator import revoke
    from ..orchestrator.grants import parse_action, parse_domain

    try:
        parsed_action, parsed_domain = parse_action(action), parse_domain(domain)
    except ValueError:
        out(f"Unknown action/domain: {action} {domain}")
        _print_vocabulary(out)
        return 2

    settings, store, matrix, audit = _resolve(settings, audit_log)
    revoke(
        store, matrix, parsed_action, parsed_domain,
        audit_log=audit, user_id=settings.user_id,
    )
    out(
        f"Revoked: {parsed_action.value} on {parsed_domain.value} — back to "
        "the READ_ONLY floor."
    )
    return 0


def run_autonomy_record(
    action: str | None = None,
    domain: str | None = None,
    *,
    settings: Any = None,
    audit_log: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    from ..orchestrator import track_records
    from ..orchestrator.grants import parse_action, parse_domain

    settings, store, matrix, audit = _resolve(settings, audit_log)
    records = track_records(audit)
    if action and domain:
        try:
            key = (parse_action(action), parse_domain(domain))
        except ValueError:
            out(f"Unknown action/domain: {action} {domain}")
            return 2
        records = {key: records[key]} if key in records else {}

    if not records:
        out("No decisions in the window yet.")
        return 0
    for record in records.values():
        out(record.render())
    return 0


def _resolve(settings: Any, audit_log: Any):
    from ..config import Settings
    from ..orchestrator import default_matrix
    from ..orchestrator.grants import JsonPermissionMatrixStore

    resolved_settings = settings or Settings.from_env()
    store = JsonPermissionMatrixStore(resolved_settings.autonomy_state_path)
    matrix = store.load() or default_matrix()
    if audit_log is None:
        from ..audit.log import JsonlAuditLog

        audit_log = JsonlAuditLog(resolved_settings.audit_log_path)
    return resolved_settings, store, matrix, audit_log


def _print_vocabulary(out: Callable[[str], None]) -> None:
    from ..orchestrator.autonomy import Action, Domain, Rung

    out("actions: " + ", ".join(a.value for a in Action))
    out("domains: " + ", ".join(d.value for d in Domain))
    out("rungs:   " + ", ".join(f"{r.name.lower()} ({int(r)})" for r in Rung))
