from attune.orchestrator.autonomy import (
    Action,
    Domain,
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
