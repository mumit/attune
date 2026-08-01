"""Pairwise judging (build prompt 27, task 2): position is randomized on
every comparison, both orders are always evaluated, and a genuine
position-swap disagreement is reported rather than silently resolved."""

from __future__ import annotations

import random

from attune.evals.judge import judge_pairwise


class _ScriptedClient:
    """Returns successive scripted WINNER lines, one per call, in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def chat_completions_create(self, **kwargs):
        self.calls.append(kwargs)
        content = self._responses.pop(0)

        class _M:
            pass

        m = _M()
        m.content = content

        class _C:
            pass

        c = _C()
        c.message = m

        class _R:
            pass

        r = _R()
        r.choices = [c]
        return r


class _AlwaysPrefersSlotA:
    """A deliberately slot-biased fake: always prefers whichever text is in
    slot A, regardless of content — a stand-in for real position bias."""

    def chat_completions_create(self, **kwargs):
        class _M:
            pass

        m = _M()
        m.content = "WINNER: A"

        class _C:
            pass

        c = _C()
        c.message = m

        class _R:
            pass

        r = _R()
        r.choices = [c]
        return r


def test_position_is_randomized_candidate_first():
    client = _ScriptedClient(["WINNER: A", "WINNER: B"])
    judge_pairwise(
        client, case_id="c1", context="ctx", candidate_text="cand", gold_text="gold",
        rng=random.Random(1),  # deterministic draw for this seed
    )
    # Whatever the draw was, forward/reversed calls must be to OPPOSITE slots.
    forward_user = client.calls[0]["messages"][1]["content"]
    reversed_user = client.calls[1]["messages"][1]["content"]
    assert forward_user != reversed_user


def test_consistent_judge_reports_a_resolved_winner():
    # A judge that always prefers "cand" regardless of slot, in both orders.
    client = _ScriptedClient(["WINNER: A", "WINNER: B"])  # candidate wins both times
    result = judge_pairwise(
        client, case_id="c1", context="ctx", candidate_text="cand", gold_text="gold",
        rng=random.Random(0),
    )
    assert result.agrees is True
    assert result.reported_winner in ("candidate", "gold", "tie")


def test_position_bias_produces_disagreement_and_unresolved_winner():
    client = _AlwaysPrefersSlotA()
    result = judge_pairwise(
        client, case_id="c1", context="ctx", candidate_text="cand", gold_text="gold",
        rng=random.Random(0),
    )
    # Always-A means forward and reversed pick different underlying texts.
    assert result.forward_winner != result.reversed_winner
    assert result.agrees is False
    assert result.reported_winner is None


def test_malformed_response_is_treated_as_tie_not_a_crash():
    client = _ScriptedClient(["not valid output at all", "still not valid"])
    result = judge_pairwise(
        client, case_id="c1", context="ctx", candidate_text="cand", gold_text="gold",
        rng=random.Random(0),
    )
    assert result.forward_winner == "tie"
    assert result.reversed_winner == "tie"
    assert result.agrees is True


def test_every_comparison_runs_both_orders():
    client = _ScriptedClient(["WINNER: TIE", "WINNER: TIE"])
    judge_pairwise(
        client, case_id="c1", context="ctx", candidate_text="cand", gold_text="gold",
        rng=random.Random(0),
    )
    assert len(client.calls) == 2
