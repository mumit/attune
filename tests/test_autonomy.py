from attune.orchestrator.autonomy import (
    Action,
    Domain,
    GrantScope,
    PermissionMatrix,
    Rung,
    default_matrix,
)


def test_unset_defaults_to_read_only():
    m = PermissionMatrix()
    assert m.max_rung(Action.SEND_REPLY, Domain.MAIL) == Rung.READ_ONLY


def test_grant_is_immutable():
    m1 = PermissionMatrix()
    m2 = m1.grant(Action.DRAFT_REPLY, Domain.MAIL, Rung.PROPOSE)
    assert m1.max_rung(Action.DRAFT_REPLY, Domain.MAIL) == Rung.READ_ONLY
    assert m2.max_rung(Action.DRAFT_REPLY, Domain.MAIL) == Rung.PROPOSE


def test_allows_respects_ceiling():
    m = default_matrix()
    # drafting mail is allowed at propose...
    assert m.allows(Action.DRAFT_REPLY, Domain.MAIL, Rung.PROPOSE)
    # ...but sending mail is not permitted at all by default
    assert not m.allows(Action.SEND_REPLY, Domain.MAIL, Rung.PROPOSE)
    assert not m.allows(Action.SEND_REPLY, Domain.MAIL, Rung.ACT_NOTIFY)


def test_default_posture_is_conservative():
    m = default_matrix()
    # nothing is autonomous out of the box
    for action in Action:
        for domain in Domain:
            assert m.max_rung(action, domain) < Rung.ACT_NOTIFY


def test_label_mail_granted_propose_by_default():
    """Phase 3 stage 1, G9: LABEL ships with a default PROPOSE grant, same
    posture as DRAFT_REPLY — proposing an archive is safe because a human
    still has to approve the card before anything is archived."""
    m = default_matrix()
    assert m.allows(Action.LABEL, Domain.MAIL, Rung.PROPOSE)
    assert not m.allows(Action.LABEL, Domain.MAIL, Rung.ACT_NOTIFY)


# ---------------------------------------------------------------------------
# Phase 4 stage 1 (G14): scoped grants
# ---------------------------------------------------------------------------
#
# GrantScope.matches — present/missing context x predicate combinations,
# the fail-closed pin front and center.


def test_none_predicate_matches_any_context_including_missing():
    scope = GrantScope()
    assert scope.matches(None, None)
    assert scope.matches("urgent", "high")
    assert scope.is_unscoped()


def test_priority_predicate_matches_only_present_and_member():
    scope = GrantScope(priorities=frozenset({"routine", "noise"}))
    assert scope.matches("routine", None)
    assert scope.matches("noise", "anything-untyped")
    assert not scope.matches("urgent", None)  # present but not a member


def test_priority_predicate_fails_closed_on_missing_context():
    """The pin: a priority-scoped grant cannot apply to an item whose
    priority is unknown. Missing context never satisfies a predicate that
    has values, however permissive the set."""
    scope = GrantScope(priorities=frozenset({"urgent", "routine", "noise"}))
    assert not scope.matches(None, None)


def test_tier_predicate_fails_closed_on_missing_context():
    scope = GrantScope(tiers=frozenset({"high"}))
    assert not scope.matches("routine", None)
    assert scope.matches("routine", "high")


def test_both_predicates_must_match():
    scope = GrantScope(priorities=frozenset({"routine"}), tiers=frozenset({"high"}))
    assert scope.matches("routine", "high")
    assert not scope.matches("routine", "normal")
    assert not scope.matches("urgent", "high")
    assert not scope.matches("routine", None)


def test_scope_with_both_none_canonicalizes_to_unscoped_on_grant():
    m = PermissionMatrix().grant(
        Action.LABEL, Domain.MAIL, Rung.PROPOSE, scope=GrantScope(None, None)
    )
    entries = m.grants[(Action.LABEL, Domain.MAIL)]
    assert len(entries) == 1
    assert entries[0].scope is None


# ---------------------------------------------------------------------------
# Multi-grant max selection
# ---------------------------------------------------------------------------


def test_max_rung_selects_highest_matching_grant():
    m = (
        PermissionMatrix()
        .grant(Action.DRAFT_REPLY, Domain.MAIL, Rung.PROPOSE)
        .grant(
            Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY,
            scope=GrantScope(priorities=frozenset({"routine"})),
        )
    )
    # unscoped floor still applies when the scoped grant doesn't match
    assert m.max_rung(Action.DRAFT_REPLY, Domain.MAIL) == Rung.PROPOSE
    assert (
        m.max_rung(Action.DRAFT_REPLY, Domain.MAIL, priority="noise")
        == Rung.PROPOSE
    )
    # the scoped grant wins when it matches (routine > the unscoped PROPOSE)
    assert (
        m.max_rung(Action.DRAFT_REPLY, Domain.MAIL, priority="routine")
        == Rung.ACT_NOTIFY
    )


def test_max_rung_ignores_non_matching_scoped_grants_entirely():
    m = PermissionMatrix().grant(
        Action.LABEL, Domain.MAIL, Rung.AUTONOMOUS,
        scope=GrantScope(tiers=frozenset({"high"})),
    )
    assert m.max_rung(Action.LABEL, Domain.MAIL, tier="low") == Rung.READ_ONLY
    assert m.max_rung(Action.LABEL, Domain.MAIL) == Rung.READ_ONLY  # no tier context


# ---------------------------------------------------------------------------
# The URGENT interrupt rule (module docstring, autonomy.py)
# ---------------------------------------------------------------------------


def test_urgent_caps_unscoped_grant_above_propose():
    """An unscoped ACT_NOTIFY grant auto-applies ROUTINE but interrupts
    (caps to PROPOSE) for URGENT — the product default."""
    m = PermissionMatrix().grant(Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY)
    assert m.max_rung(Action.DRAFT_REPLY, Domain.MAIL, priority="routine") == Rung.ACT_NOTIFY
    assert m.max_rung(Action.DRAFT_REPLY, Domain.MAIL, priority="urgent") == Rung.PROPOSE
    assert m.allows(Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY, priority="urgent") is False


def test_urgent_does_not_cap_propose_itself():
    m = PermissionMatrix().grant(Action.DRAFT_REPLY, Domain.MAIL, Rung.PROPOSE)
    assert m.max_rung(Action.DRAFT_REPLY, Domain.MAIL, priority="urgent") == Rung.PROPOSE


def test_explicit_urgent_scope_overrides_the_cap():
    """A grant that deliberately lists 'urgent' in its scope is exempt from
    the cap — the one way to auto-act on urgent items, and it must be
    written explicitly, never implied."""
    m = PermissionMatrix().grant(
        Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY,
        scope=GrantScope(priorities=frozenset({"urgent"})),
    )
    assert m.max_rung(Action.DRAFT_REPLY, Domain.MAIL, priority="urgent") == Rung.ACT_NOTIFY


def test_urgent_cap_applies_per_grant_before_max():
    """A routine-scoped ACT_NOTIFY grant does not leak autonomy into an
    urgent context just because another unscoped PROPOSE grant exists for
    the same pair — capping happens per matching grant, not after the max."""
    m = (
        PermissionMatrix()
        .grant(Action.DRAFT_REPLY, Domain.MAIL, Rung.PROPOSE)
        .grant(
            Action.DRAFT_REPLY, Domain.MAIL, Rung.ACT_NOTIFY,
            scope=GrantScope(priorities=frozenset({"routine"})),
        )
    )
    assert m.max_rung(Action.DRAFT_REPLY, Domain.MAIL, priority="urgent") == Rung.PROPOSE


# ---------------------------------------------------------------------------
# grant/revoke by scope
# ---------------------------------------------------------------------------


def test_grant_replaces_same_scope_appends_different_scope():
    scope_a = GrantScope(priorities=frozenset({"routine"}))
    scope_b = GrantScope(priorities=frozenset({"noise"}))
    m = PermissionMatrix().grant(Action.LABEL, Domain.MAIL, Rung.PROPOSE, scope=scope_a)
    m = m.grant(Action.LABEL, Domain.MAIL, Rung.ACT_NOTIFY, scope=scope_a)  # replace
    m = m.grant(Action.LABEL, Domain.MAIL, Rung.AUTONOMOUS, scope=scope_b)  # append
    entries = m.grants[(Action.LABEL, Domain.MAIL)]
    assert len(entries) == 2
    by_scope = {sg.scope: sg.rung for sg in entries}
    assert by_scope[scope_a] == Rung.ACT_NOTIFY
    assert by_scope[scope_b] == Rung.AUTONOMOUS


def test_revoke_with_scope_removes_only_that_grant():
    scope = GrantScope(priorities=frozenset({"routine"}))
    m = (
        PermissionMatrix()
        .grant(Action.LABEL, Domain.MAIL, Rung.PROPOSE)  # unscoped
        .grant(Action.LABEL, Domain.MAIL, Rung.ACT_NOTIFY, scope=scope)
    )
    m2 = m.revoke(Action.LABEL, Domain.MAIL, scope=scope)
    assert m2.max_rung(Action.LABEL, Domain.MAIL, priority="routine") == Rung.PROPOSE
    assert len(m2.grants[(Action.LABEL, Domain.MAIL)]) == 1


def test_revoke_without_scope_removes_every_grant_for_the_pair():
    scope = GrantScope(priorities=frozenset({"routine"}))
    m = (
        PermissionMatrix()
        .grant(Action.LABEL, Domain.MAIL, Rung.PROPOSE)
        .grant(Action.LABEL, Domain.MAIL, Rung.ACT_NOTIFY, scope=scope)
    )
    m2 = m.revoke(Action.LABEL, Domain.MAIL)
    assert (Action.LABEL, Domain.MAIL) not in m2.grants
    assert m2.max_rung(Action.LABEL, Domain.MAIL, priority="routine") == Rung.READ_ONLY


def test_revoke_scope_none_removes_only_the_unscoped_entry():
    scope = GrantScope(priorities=frozenset({"routine"}))
    m = (
        PermissionMatrix()
        .grant(Action.LABEL, Domain.MAIL, Rung.PROPOSE)  # unscoped
        .grant(Action.LABEL, Domain.MAIL, Rung.ACT_NOTIFY, scope=scope)
    )
    m2 = m.revoke(Action.LABEL, Domain.MAIL, scope=None)
    remaining = m2.grants[(Action.LABEL, Domain.MAIL)]
    assert len(remaining) == 1
    assert remaining[0].scope == scope


# ---------------------------------------------------------------------------
# Zero-context back-compat: byte-identical to pre-scoping behavior
# ---------------------------------------------------------------------------


def test_zero_context_calls_only_see_unscoped_grants():
    """A scoped grant is invisible to every pre-Phase-4 call site (which
    never passes priority/tier) — fail-closed matching against missing
    context, not a special case."""
    m = default_matrix().grant(
        Action.LABEL, Domain.MAIL, Rung.AUTONOMOUS,
        scope=GrantScope(priorities=frozenset({"noise"})),
    )
    assert m.max_rung(Action.LABEL, Domain.MAIL) == Rung.PROPOSE  # default_matrix's own grant
    assert not m.allows(Action.LABEL, Domain.MAIL, Rung.ACT_NOTIFY)


def test_default_matrix_default_posture_unchanged_by_scoping_support():
    """Every existing default_matrix() assertion still holds with the
    multi-grant data model underneath — same test as
    test_default_posture_is_conservative, restated here to pin it
    explicitly against the Phase 4 stage 1 change."""
    m = default_matrix()
    for action in Action:
        for domain in Domain:
            assert m.max_rung(action, domain) < Rung.ACT_NOTIFY
    assert m.allows(Action.DRAFT_REPLY, Domain.MAIL, Rung.PROPOSE)
    assert not m.allows(Action.SEND_REPLY, Domain.MAIL, Rung.PROPOSE)
