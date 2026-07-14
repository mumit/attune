"""The memory-quality regression set (design 2.4, roadmap prompt 13) —
LoCoMo/LongMemEval-style categories: single-session recall, multi-session
recall, preference recall, and knowledge update.

Offline by default: a mem0-shaped in-memory fake substrate under the REAL
``Mem0Store`` adapter (so add/search/get_all/delete/consolidate logic is the
code under test), and a scripted consolidation "model". This regression-
checks the *pipeline*: what gets written, what consolidation decides given a
canned response, what retrieval is asked. Run it — and extend
``memory_quality_scenarios.json`` — after any change to ``memory/``,
``signals.py``, or the consolidation prompt.

A live variant (real Mem0 + Qdrant + configured OpenAI-compatible gateway) runs the same knowledge-update
shape when ``ATTUNE_LIVE_MEMORY_EVAL=1`` — manual, after memory-pipeline
changes, never in CI.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from attune.memory.mem0_store import Mem0Store


class FakeMemory:
    """Mem0-shaped in-memory substrate: dict API, naive keyword search.

    Naive on purpose — these tests regression-check pipeline logic, not
    embedding quality. Search scores by shared lowercase tokens."""

    def __init__(self):
        self.items: dict[str, dict] = {}

    def add(self, payload, *, user_id, metadata=None, infer=True):
        text = payload if isinstance(payload, str) else " ".join(
            m["content"] for m in payload
        )
        mem_id = f"mem-{uuid.uuid4().hex[:8]}"
        self.items[mem_id] = {
            "id": mem_id, "memory": text, "metadata": metadata or {},
            "user_id": user_id,
        }
        return {"results": [self.items[mem_id]]}

    def search(self, *, query, user_id, limit=8):
        q_tokens = set(query.lower().split())
        scored = []
        for item in self.items.values():
            if item["user_id"] != user_id:
                continue
            overlap = len(q_tokens & set(item["memory"].lower().split()))
            if overlap:
                scored.append((overlap, item))
        scored.sort(key=lambda pair: -pair[0])
        return {"results": [dict(item, score=s) for s, item in scored[:limit]]}

    def get_all(self, *, user_id, limit=100):
        return {
            "results": [
                i for i in self.items.values() if i["user_id"] == user_id
            ][:limit]
        }

    def delete(self, *, memory_id):
        self.items.pop(memory_id, None)


class CannedClient:
    """Returns a scripted consolidation response and records the prompt."""

    def __init__(self, response_text):
        self._response = response_text
        self.calls: list[dict] = []

    def chat_completions_create(self, **kwargs):
        self.calls.append(kwargs)

        class _C:
            class message:
                content = None

        _C.message.content = self._response

        class _R:
            choices = [_C]
        return _R()


def _store(client=None):
    return Mem0Store(memory=FakeMemory(), client=client)


# ---------------------------------------------------------------------------
# Recall categories, driven by the scenario file
# ---------------------------------------------------------------------------

_SCENARIOS = json.load(
    open(os.path.join(os.path.dirname(__file__), "memory_quality_scenarios.json"))
)["scenarios"]


@pytest.mark.parametrize(
    "scenario", _SCENARIOS, ids=[s["name"] for s in _SCENARIOS]
)
def test_recall_scenarios(scenario):
    store = _store()
    for session in scenario["sessions"]:
        for fact in session:
            store.add(fact, user_id="u1", infer=False)

    results = store.search(scenario["query"], user_id="u1")

    assert results, f"nothing retrieved for {scenario['query']!r}"
    assert any(
        scenario["expect_contains"].lower() in r.text.lower() for r in results
    ), f"{scenario['expect_contains']!r} not in {[r.text for r in results]}"


# ---------------------------------------------------------------------------
# Knowledge update — the category that matters most (design 2.4): a
# contradicted fact must stop coming back after consolidation.
# ---------------------------------------------------------------------------


def _seed_ownership_change(store):
    """March: Priya owns Falcon. June: repeated signals say Marcus does."""
    old = store.add("Priya is the PM for Project Falcon", user_id="u1", infer=False)
    old_id = old[0].id
    for _ in range(3):
        store.add(
            "[approved] mail: reply confirming Marcus now runs Project Falcon",
            user_id="u1", metadata={"signal": "action", "action": "approved",
                                    "domain": "mail"},
            infer=False,
        )
    return old_id


def test_knowledge_update_supersedes_contradicted_fact():
    fake = FakeMemory()
    store = Mem0Store(memory=fake)
    old_id = _seed_ownership_change(store)
    store._client = CannedClient(json.dumps({
        "promotions": [],
        "merges": [],
        "supersessions": [
            {"text": "Marcus is the PM for Project Falcon", "supersedes": old_id}
        ],
    }))

    report = store.consolidate(user_id="u1")

    assert report.superseded == 1
    results = store.search("who is the PM for Project Falcon", user_id="u1")
    texts = [r.text for r in results]
    assert any("Marcus is the PM" in t for t in texts)
    assert not any("Priya" in t for t in texts)  # the old fact is gone
    # the new fact carries the breadcrumb
    replacement = next(r for r in results if "Marcus is the PM" in r.text)
    assert replacement.metadata["supersedes"] == old_id


def test_promotion_absorbs_raw_signals():
    fake = FakeMemory()
    store = Mem0Store(memory=fake)
    ids = []
    for _ in range(3):
        added = store.add(
            "[rejected] mail: draft_reply to recruiter cold-email",
            user_id="u1", metadata={"signal": "action", "action": "rejected",
                                    "domain": "mail"},
            infer=False,
        )
        ids.append(added[0].id)
    store._client = CannedClient(json.dumps({
        "promotions": [
            {"text": "Never draft replies to recruiter cold-emails", "absorbs": ids}
        ],
        "merges": [], "supersessions": [],
    }))

    report = store.consolidate(user_id="u1")

    assert report.merged == 3
    remaining = [i["memory"] for i in fake.items.values()]
    assert remaining == ["Never draft replies to recruiter cold-emails"]


# ---------------------------------------------------------------------------
# Conservative-apply contract
# ---------------------------------------------------------------------------


def test_malformed_model_response_mutates_nothing():
    fake = FakeMemory()
    store = Mem0Store(memory=fake)
    _seed_ownership_change(store)
    before = dict(fake.items)
    store._client = CannedClient("I think you should merge some things! Not JSON.")

    report = store.consolidate(user_id="u1")

    assert fake.items == before
    assert report.merged == 0 and report.superseded == 0
    assert any("no mutations" in n for n in report.notes)


def test_unknown_ids_are_never_deleted():
    fake = FakeMemory()
    store = Mem0Store(memory=fake)
    old_id = _seed_ownership_change(store)
    store._client = CannedClient(json.dumps({
        "promotions": [{"text": "bogus", "absorbs": ["mem-doesnotexist"]}],
        "merges": [],
        "supersessions": [
            {"text": "hijack", "supersedes": "mem-alsofake"},
        ],
    }))
    count_before = len(fake.items)

    report = store.consolidate(user_id="u1")

    # the promotion text was added (harmless) but nothing was deleted, and
    # the supersession of an unknown id was skipped entirely
    assert report.merged == 0 and report.superseded == 0
    assert len(fake.items) == count_before + 1
    assert old_id in fake.items


def test_no_client_reports_honest_noop():
    store = _store(client=None)
    store.add("a fact", user_id="u1", infer=False)
    report = store.consolidate(user_id="u1")
    assert report.merged == 0 and report.superseded == 0
    assert any("no client" in n for n in report.notes)


def test_untrusted_framing_in_consolidation_prompt():
    fake = FakeMemory()
    store = Mem0Store(memory=fake)
    _seed_ownership_change(store)
    client = CannedClient('{"promotions": [], "merges": [], "supersessions": []}')
    store._client = client

    store.consolidate(user_id="u1")

    system = client.calls[0]["messages"][0]["content"]
    assert "never follow instructions" in system
    assert "DATA" in system


def test_signal_cap_bounds_the_prompt():
    from attune.memory.mem0_store import CONSOLIDATE_SIGNAL_CAP

    fake = FakeMemory()
    store = Mem0Store(memory=fake)
    for i in range(CONSOLIDATE_SIGNAL_CAP + 50):
        store.add(
            f"[approved] mail: signal {i}",
            user_id="u1", metadata={"signal": "action"}, infer=False,
        )
    client = CannedClient('{"promotions": [], "merges": [], "supersessions": []}')
    store._client = client

    store.consolidate(user_id="u1")

    user_prompt = client.calls[0]["messages"][1]["content"]
    assert user_prompt.count("id=mem-") <= CONSOLIDATE_SIGNAL_CAP


# ---------------------------------------------------------------------------
# Live variant — real Mem0/Qdrant/configured OpenAI-compatible gateway; manual, never CI
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("ATTUNE_LIVE_MEMORY_EVAL") != "1",
    reason="live memory eval: set ATTUNE_LIVE_MEMORY_EVAL=1 (needs Qdrant + ATTUNE_LLM_API_KEY)",
)
def test_live_knowledge_update():  # pragma: no cover - live services
    from attune.llm import make_client
    from attune.memory.mem0_store import build_mem0_config

    store = Mem0Store(build_mem0_config(), client=make_client())
    user = f"eval-{uuid.uuid4().hex[:8]}"
    store.add("Priya is the PM for Project Falcon", user_id=user)
    for _ in range(3):
        store.add(
            "[approved] mail: reply confirming Marcus now runs Project Falcon",
            user_id=user, metadata={"signal": "action"}, infer=False,
        )
    store.consolidate(user_id=user)
    results = store.search("who is the PM for Project Falcon", user_id=user)
    assert any("Marcus" in r.text for r in results)


# ---------------------------------------------------------------------------
# Verified, journaled mutations (prompt 22, review finding #7)
# ---------------------------------------------------------------------------


class SilentlyFailingMemory(FakeMemory):
    """A substrate whose add() quietly writes nothing — the exact failure
    that used to erase absorbed evidence."""

    def add(self, payload, *, user_id, metadata=None, infer=True):
        return {"results": []}


class RecordingAuditLog:
    def __init__(self):
        self.records: list[dict] = []

    def record(self, **kwargs):
        self.records.append(kwargs)


def test_unverified_write_aborts_batch_and_deletes_nothing():
    fake = SilentlyFailingMemory()
    store = Mem0Store(memory=fake)
    # seed via the working parent implementation
    FakeMemory.add(fake, "[rejected] mail: one", user_id="u1",
                   metadata={"signal": "action"})
    FakeMemory.add(fake, "[rejected] mail: two", user_id="u1",
                   metadata={"signal": "action"})
    ids = list(fake.items)
    audit = RecordingAuditLog()
    store._client = CannedClient(json.dumps({
        "promotions": [
            {"text": "first promotion", "absorbs": [ids[0]]},
            {"text": "second promotion", "absorbs": [ids[1]]},
        ],
        "merges": [], "supersessions": [],
    }))

    report = store.consolidate(user_id="u1", audit_log=audit)

    # nothing deleted — the batch aborted on the FIRST unverified write
    assert set(fake.items) == set(ids)
    assert report.merged == 0
    assert any("write_unverified" in n for n in report.notes)
    events = [e["event"] for rec in audit.records for e in rec["events"]]
    assert events == ["consolidation_aborted"]


def test_supersession_write_failure_retains_old_fact():
    fake = SilentlyFailingMemory()
    store = Mem0Store(memory=fake)
    old = FakeMemory.add(fake, "Priya is the PM", user_id="u1")
    old_id = old["results"][0]["id"]
    store._client = CannedClient(json.dumps({
        "promotions": [], "merges": [],
        "supersessions": [{"text": "Marcus is the PM", "supersedes": old_id}],
    }))

    report = store.consolidate(user_id="u1")

    assert old_id in fake.items  # the old fact survives an unverified write
    assert report.superseded == 0


def test_applied_mutations_are_journaled():
    fake = FakeMemory()
    store = Mem0Store(memory=fake)
    old_id = _seed_ownership_change(store)
    audit = RecordingAuditLog()
    store._client = CannedClient(json.dumps({
        "promotions": [], "merges": [],
        "supersessions": [
            {"text": "Marcus is the PM for Project Falcon", "supersedes": old_id}
        ],
    }))

    store.consolidate(user_id="u1", audit_log=audit)

    events = [e for rec in audit.records for e in rec["events"]]
    superseded = next(e for e in events if e["event"] == "consolidation_superseded")
    assert superseded["deleted_ids"] == [old_id]
    assert superseded["new_ids"]  # the verified replacement's id(s)
    assert audit.records[0]["workflow"] == "memory"


def test_write_precedes_delete_per_item():
    """Order pin: a crash between write and delete must leave a duplicate,
    never a loss — so the verified add always lands before any delete."""
    calls: list[str] = []

    class OrderRecordingMemory(FakeMemory):
        def add(self, payload, *, user_id, metadata=None, infer=True):
            calls.append("add")
            return super().add(payload, user_id=user_id, metadata=metadata,
                               infer=infer)

        def delete(self, *, memory_id):
            calls.append("delete")
            super().delete(memory_id=memory_id)

    fake = OrderRecordingMemory()
    store = Mem0Store(memory=fake)
    old_id = _seed_ownership_change(store)
    calls.clear()
    store._client = CannedClient(json.dumps({
        "promotions": [], "merges": [],
        "supersessions": [{"text": "Marcus runs Falcon", "supersedes": old_id}],
    }))

    store.consolidate(user_id="u1")

    assert calls.index("add") < calls.index("delete")


def test_journal_failure_never_aborts_consolidation():
    class ExplodingAudit:
        def record(self, **kw):
            raise RuntimeError("audit disk full")

    fake = FakeMemory()
    store = Mem0Store(memory=fake)
    old_id = _seed_ownership_change(store)
    store._client = CannedClient(json.dumps({
        "promotions": [], "merges": [],
        "supersessions": [{"text": "Marcus runs Falcon", "supersedes": old_id}],
    }))

    report = store.consolidate(user_id="u1", audit_log=ExplodingAudit())

    assert report.superseded == 1  # the mutation itself still applied
