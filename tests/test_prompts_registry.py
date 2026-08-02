"""The versioned prompt registry (build prompt 36, docs/plan-2026-h2.md
P10): current()/promote()/revert()/history() over an immutable, append-only
JSON store — the mechanism that makes a promotion (or a revert) actually
change what production sends, and traceable back to the exact text a
ledger row's ``prompt_version`` names."""

from __future__ import annotations

import json
import os

from attune.prompts import PROMPT_DRAFT, current, history, promote, revert


def test_current_is_the_baseline_when_nothing_promoted(tmp_path):
    resolved = current(PROMPT_DRAFT, versions_dir=str(tmp_path))
    assert resolved == PROMPT_DRAFT


def test_promote_bumps_version_and_current_reflects_it(tmp_path):
    versions_dir = str(tmp_path)
    promoted = promote(
        PROMPT_DRAFT, "NEW TEXT", source="gepa",
        scorer_deltas={"edit_burden_proxy": -0.02}, versions_dir=versions_dir,
    )
    assert promoted.version == PROMPT_DRAFT.version + 1
    assert promoted.stable_prefix == "NEW TEXT"
    assert current(PROMPT_DRAFT, versions_dir=versions_dir) == promoted


def test_promote_twice_never_overwrites_the_first_record(tmp_path):
    versions_dir = str(tmp_path)
    v2 = promote(PROMPT_DRAFT, "v2 text", source="gepa", versions_dir=versions_dir)
    v3 = promote(PROMPT_DRAFT, "v3 text", source="gepa", versions_dir=versions_dir)
    assert v2.version == PROMPT_DRAFT.version + 1
    assert v3.version == PROMPT_DRAFT.version + 2

    records = history(PROMPT_DRAFT.name, versions_dir=versions_dir)
    assert [r.version for r in records] == [v2.version, v3.version]
    assert records[0].stable_prefix == "v2 text"  # untouched by the later promotion
    assert records[1].parent_version == v2.version


def test_promoted_version_is_a_committed_json_file_on_disk(tmp_path):
    versions_dir = str(tmp_path)
    promote(PROMPT_DRAFT, "on disk", source="gepa", versions_dir=versions_dir)
    path = os.path.join(versions_dir, f"{PROMPT_DRAFT.name}.json")
    assert os.path.exists(path)
    with open(path) as f:
        raw = json.load(f)
    assert raw["records"][0]["stable_prefix"] == "on disk"


def test_revert_appends_a_new_version_rather_than_deleting_the_bad_one(tmp_path):
    versions_dir = str(tmp_path)
    promote(PROMPT_DRAFT, "bad candidate", source="gepa", versions_dir=versions_dir)
    reverted = revert(PROMPT_DRAFT, PROMPT_DRAFT.version, versions_dir=versions_dir)

    assert reverted.version == PROMPT_DRAFT.version + 2
    assert reverted.stable_prefix == PROMPT_DRAFT.stable_prefix
    records = history(PROMPT_DRAFT.name, versions_dir=versions_dir)
    assert len(records) == 2  # the bad version is still in the history, not deleted
    assert records[0].stable_prefix == "bad candidate"
    assert records[1].source == "revert"


def test_reverting_the_version_reverts_behaviour(tmp_path):
    """The acceptance-mandated property: a caller resolving ``current()``
    before and after a promote-then-revert cycle sees the SAME text again —
    i.e. reverting a prompt version actually reverts what production would
    send, not just a label in a history file."""
    versions_dir = str(tmp_path)
    before = current(PROMPT_DRAFT, versions_dir=versions_dir)
    promote(PROMPT_DRAFT, "a change nobody wanted", source="gepa", versions_dir=versions_dir)
    assert current(PROMPT_DRAFT, versions_dir=versions_dir).stable_prefix != before.stable_prefix

    revert(PROMPT_DRAFT, before.version, versions_dir=versions_dir)
    after = current(PROMPT_DRAFT, versions_dir=versions_dir)
    assert after.stable_prefix == before.stable_prefix


def test_ledger_row_prompt_version_is_traceable_back_to_exact_text(tmp_path):
    """A ledger row only ever carries a bare ``prompt_version`` int (see
    ``orchestrator.ledger.LedgerRow.prompt_version``). Given that int and the
    prompt's name, the exact stable_prefix that produced it must be
    recoverable from the versioned store."""
    versions_dir = str(tmp_path)
    promoted = promote(PROMPT_DRAFT, "the exact text a draft call used", source="gepa", versions_dir=versions_dir)

    # Simulate what a ledger row would have recorded for a draft made under
    # this version.
    recorded_prompt_version = promoted.version

    records = history(PROMPT_DRAFT.name, versions_dir=versions_dir)
    match = next(r for r in records if r.version == recorded_prompt_version)
    assert match.stable_prefix == "the exact text a draft call used"


def test_revert_to_a_version_that_does_not_exist_raises(tmp_path):
    versions_dir = str(tmp_path)
    try:
        revert(PROMPT_DRAFT, 999, versions_dir=versions_dir)
        assert False, "expected ValueError"
    except ValueError:
        pass
