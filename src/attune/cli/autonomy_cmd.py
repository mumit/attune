"""``attune autonomy`` — see and change the autonomy posture, and see
the track record that earns graduations (roadmap prompt 12).

This is deliberately the primary surface that can grant or revoke (rule 3):
a chat channel that relays untrusted content must never be able to escalate
autonomy, so chat gets show-and-suggest only. Parsing is strict — a typo
errors, never silently defaults. Phase 4 stage 2 (G13) amends this with ONE
narrow, ceilinged exception — a human tapping "approve" on a graduation/
demotion card in the allowlisted approval channel, never SEND_REPLY, never
above ACT_NOTIFY, always revocable via this CLI — see
``orchestrator.grants.resolve_autonomy_card`` and docs/decisions.md.

Phase 4 stage 2 (G15): granting ``send_reply`` now REFUSES outright (non-
zero exit) rather than warning, while sending isn't structurally enabled
(``ATTUNE_MAIL_SEND_ENABLED`` + a real gmail.send scope) — rule 4's "no
shortcuts" extends to not even persisting an inert grant.

Scoped grants (Phase 4 stage 1, G14): ``grant``/``revoke`` accept optional
``--priority urgent,routine,noise`` / ``--tier high,normal,low`` flags,
narrowing the grant to a :class:`~orchestrator.autonomy.GrantScope`
predicate over triage priority and sender importance tier. A grant without
either flag is the unscoped grant — matches any context, same as before this
stage. ``revoke`` without scope flags claws back EVERY grant held for the
(action, domain) pair (unchanged behavior); with scope flags, it claws back
only the matching-scope grant. ``show`` renders each grant's scope readably
and notes the urgent-interrupt rule (autonomy.py) whenever an unscoped
grant sits at ACT_NOTIFY or above: that grant still interrupts for URGENT
items unless a human deliberately writes "urgent" into its scope.
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
    priority: str | None = None,
    tier: str | None = None,
    settings: Any = None,
    audit_log: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    from ..orchestrator import grant, render_scope
    from ..orchestrator.autonomy import Action, Rung
    from ..orchestrator.grants import parse_action, parse_domain, parse_rung, parse_scope

    try:
        parsed = (parse_action(action), parse_domain(domain), parse_rung(rung))
        scope = parse_scope(priority, tier)
    except (ValueError, KeyError):
        out(
            f"Unknown action/domain/rung, or an invalid/empty --priority/"
            f"--tier scope: {action} {domain} {rung} "
            f"(--priority {priority!r} --tier {tier!r})"
        )
        _print_vocabulary(out)
        return 2

    settings, store, matrix, audit = _resolve(settings, audit_log)

    # Phase 4 stage 2 (G15): SEND_REPLY refuses outright — no shortcuts
    # (rule 4) — while sending isn't structurally enabled, rather than
    # granting an inert entry and merely warning about it (the old
    # behavior). Named, actionable: the exact flag and scope it's missing.
    if parsed[0] == Action.SEND_REPLY and not settings.mail_send_enabled:
        out(
            "Refused: send_reply autonomy requires ATTUNE_MAIL_SEND_ENABLED=1 "
            "(and a real gmail.send OAuth scope) before any grant can take "
            "effect — sending stays structurally disabled otherwise, so this "
            "grant would be inert. Set ATTUNE_MAIL_SEND_ENABLED=1, confirm "
            "with `attune doctor`, then grant again."
        )
        return 3

    grant(
        store, matrix, *parsed, scope=scope, audit_log=audit,
        user_id=settings.user_id,
    )
    scope_note = f" scoped to {render_scope(scope)}" if scope is not None else ""
    out(
        f"Granted: {parsed[0].value} on {parsed[1].value} at "
        f"{parsed[2].name}{scope_note} (persisted to "
        f"{settings.autonomy_state_path})"
    )
    if scope is None and parsed[2] >= Rung.ACT_NOTIFY:
        out(
            "Note: this unscoped grant still interrupts for URGENT priority "
            "— the urgent-interrupt rule (attune autonomy show) caps it to "
            "PROPOSE unless a grant's scope explicitly lists 'urgent'."
        )
    return 0


def run_autonomy_revoke(
    action: str,
    domain: str,
    *,
    priority: str | None = None,
    tier: str | None = None,
    settings: Any = None,
    audit_log: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    from ..orchestrator import render_scope, revoke
    from ..orchestrator.grants import parse_action, parse_domain, parse_scope

    try:
        parsed_action, parsed_domain = parse_action(action), parse_domain(domain)
        scope_given = bool(priority or tier)
        scope = parse_scope(priority, tier) if scope_given else None
    except ValueError:
        out(
            f"Unknown action/domain, or an invalid/empty --priority/--tier "
            f"scope: {action} {domain} (--priority {priority!r} --tier {tier!r})"
        )
        _print_vocabulary(out)
        return 2

    settings, store, matrix, audit = _resolve(settings, audit_log)
    if scope_given:
        revoke(
            store, matrix, parsed_action, parsed_domain, scope=scope,
            audit_log=audit, user_id=settings.user_id,
        )
        out(
            f"Revoked: {parsed_action.value} on {parsed_domain.value} scoped "
            f"to {render_scope(scope)} — that grant only."
        )
    else:
        revoke(
            store, matrix, parsed_action, parsed_domain,
            audit_log=audit, user_id=settings.user_id,
        )
        out(
            f"Revoked: {parsed_action.value} on {parsed_domain.value} — back "
            "to the READ_ONLY floor (every grant for this pair removed)."
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
