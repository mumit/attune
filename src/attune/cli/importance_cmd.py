"""``attune importance`` — inspect and correct the deterministic per-sender
importance profile (Phase 1, ``docs/future-state.md``; gaps G5/G6).

This is the "why did you rank this sender high?" answer the design calls
for: ``show`` prints the grounded reason plus the recorded (non-expired)
signals behind it, and ``list`` gives the same answer for every known
sender at a glance.

Only this CLI can pin/unpin (rule 3's posture, mirrored from
``autonomy_cmd.py``): a chat channel that relays untrusted content must not
get a mutation path here either. This stage wires the CLI only; chat gets
no ``importance`` surface at all yet, not even show-only — that can follow
the autonomy precedent later if it's wanted.
"""

from __future__ import annotations

from typing import Any, Callable


def run_importance_list(
    *,
    settings: Any = None,
    importance_profile: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    _settings, profile = _resolve(settings, importance_profile)
    senders = profile.senders()
    if not senders:
        out("No senders recorded yet.")
        return 0
    out(f"{'sender':<36} {'tier':<8} {'pinned':<7} {'signals':<8} reason")
    for sender in senders:
        assessment = profile.assess(sender)
        count = len(profile.recent_signals(sender))
        pin_mark = "yes" if assessment.pinned else ""
        out(
            f"{sender:<36} {assessment.tier.value:<8} {pin_mark:<7} "
            f"{count:<8} {assessment.reason}"
        )
    return 0


def run_importance_show(
    sender: str,
    *,
    settings: Any = None,
    importance_profile: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    _settings, profile = _resolve(settings, importance_profile)
    assessment = profile.assess(sender)
    marker = " (pinned)" if assessment.pinned else ""
    out(f"{sender}: {assessment.tier.value}{marker}")
    out(f"  reason: {assessment.reason}")
    signals = profile.recent_signals(sender)
    if not signals:
        out("  no recorded (non-expired) signals")
    else:
        out("  recorded signals (oldest first):")
        for signal, ts in signals:
            out(f"    {ts.isoformat()}  {signal.value}")
    return 0


def run_importance_pin(
    sender: str,
    tier: str,
    *,
    settings: Any = None,
    importance_profile: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    from ..orchestrator.importance import ImportanceTier

    _settings, profile = _resolve(settings, importance_profile)
    try:
        parsed_tier = ImportanceTier(tier.strip().lower())
    except ValueError:
        out(f"Unknown tier: {tier}")
        out("tiers: " + ", ".join(t.value for t in ImportanceTier))
        return 2
    profile.pin(sender, parsed_tier)
    out(
        f"Pinned {sender}: {parsed_tier.value} "
        f"(persisted to {_settings.importance_profile_path})"
    )
    return 0


def run_importance_unpin(
    sender: str,
    *,
    settings: Any = None,
    importance_profile: Any = None,
    out: Callable[[str], None] = print,
) -> int:
    _settings, profile = _resolve(settings, importance_profile)
    if profile.unpin(sender):
        out(f"Unpinned {sender}.")
    else:
        out(f"{sender} had no pin set.")
    return 0


def _resolve(settings: Any, importance_profile: Any):
    from ..config import Settings
    from ..orchestrator.importance import JsonImportanceProfile

    resolved_settings = settings or Settings.from_env()
    profile = importance_profile or JsonImportanceProfile(
        resolved_settings.importance_profile_path
    )
    return resolved_settings, profile
