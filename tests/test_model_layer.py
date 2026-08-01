"""Build prompt 28 — model layer floor: prompt registry, capability probing,
prompt caching, structured output, native tool calling, call hygiene, and the
decision ledger's new token/cache-hit visibility.

Every capability defaults off; the request shape produced at those defaults
is the acceptance bar (docs/plan-2026-h2.md P3): byte-identical to what each
call site sent before this module existed.
"""

from __future__ import annotations

import json

import pytest

from attune.config import Settings
from attune.llm import ModelCapabilities, call_with_retry, resolve_capabilities
from attune.orchestrator.ledger import (
    LedgerRow,
    compute_metrics_slice,
    record_proposal,
    render_metrics_table,
)
from attune.orchestrator.triage import Priority, _parse_triage_response, triage_thread
from attune.prompts import PROMPT_BRIEF, PROMPT_DRAFT, PROMPT_TRIAGE, render_system_message


class _FakeChatClient:
    """Records every ``chat.completions.create``-shaped call it receives."""

    def __init__(self, reply="ok", *, tool_calls=None):
        self.reply = reply
        self.tool_calls = tool_calls
        self.calls: list[dict] = []

    def chat_completions_create(self, **kwargs):
        self.calls.append(kwargs)

        class _Msg:
            content = self.reply
            tool_calls = self.tool_calls

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]
            usage = None

        return _Resp()


# ---------------------------------------------------------------------------
# Capability resolution: off by default, and reads from Settings when set.
# ---------------------------------------------------------------------------


def test_capabilities_default_off():
    assert resolve_capabilities(Settings.from_env({})) == ModelCapabilities()


def test_capabilities_resolve_from_settings():
    settings = Settings.from_env({
        "ATTUNE_MODEL_SUPPORTS_TOOLS": "true",
        "ATTUNE_MODEL_SUPPORTS_STRUCTURED_OUTPUT": "true",
        "ATTUNE_MODEL_SUPPORTS_PROMPT_CACHE": "true",
        "ATTUNE_MODEL_MAX_TOKENS": "500",
        "ATTUNE_MODEL_TIMEOUT_SECONDS": "12.5",
        "ATTUNE_MODEL_MAX_RETRIES": "3",
    })
    caps = resolve_capabilities(settings)
    assert caps == ModelCapabilities(
        supports_tools=True, supports_structured_output=True, supports_prompt_cache=True,
        max_tokens=500, timeout_seconds=12.5, max_retries=3,
    )


# ---------------------------------------------------------------------------
# Prompt caching: render_system_message.
# ---------------------------------------------------------------------------


def test_render_system_message_off_is_byte_identical_concatenation():
    msg = render_system_message("STABLE.", "VOLATILE.", capabilities=ModelCapabilities())
    assert msg == {"role": "system", "content": "STABLE.VOLATILE."}


def test_render_system_message_on_splits_stable_prefix_as_cacheable_block():
    caps = ModelCapabilities(supports_prompt_cache=True)
    msg = render_system_message("STABLE.", "VOLATILE.", capabilities=caps)
    assert msg["role"] == "system"
    assert msg["content"] == [
        {"type": "text", "text": "STABLE.", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "VOLATILE."},
    ]


def test_render_system_message_on_omits_empty_volatile_part():
    caps = ModelCapabilities(supports_prompt_cache=True)
    msg = render_system_message("STABLE.", capabilities=caps)
    assert msg["content"] == [
        {"type": "text", "text": "STABLE.", "cache_control": {"type": "ephemeral"}},
    ]


# ---------------------------------------------------------------------------
# Byte-identical gate-off request shape (acceptance criterion 1).
# ---------------------------------------------------------------------------


def test_triage_gate_off_request_matches_pre_registry_shape():
    client = _FakeChatClient("PRIORITY: ROUTINE\nREASON: fine.")
    triage_thread(client, "hello", capabilities=ModelCapabilities())

    kwargs = client.calls[0]
    assert set(kwargs) == {"model", "messages"}
    system = kwargs["messages"][0]
    assert isinstance(system["content"], str)
    assert system["content"] == PROMPT_TRIAGE.stable_prefix


def test_default_draft_fn_gate_off_request_matches_pre_registry_shape():
    from attune.orchestrator.draft_approve import _default_draft_fn

    client = _FakeChatClient("Sure, sounds good.")
    text = _default_draft_fn(client, "hi", [], "mail", capabilities=ModelCapabilities())

    assert text == "Sure, sounds good."
    kwargs = client.calls[0]
    assert set(kwargs) == {"model", "messages"}
    system = kwargs["messages"][0]
    assert isinstance(system["content"], str)
    assert system["content"].startswith(PROMPT_DRAFT.stable_prefix)


def test_interaction_planner_gate_off_request_carries_no_tool_kwargs():
    from attune.interaction import plan_interaction

    client = _FakeChatClient("INTENT: GENERAL\nGMAIL_QUERY: NONE\nSTART: NONE\nEND: NONE")
    plan_interaction(client, "how's it going", timezone_name="UTC", capabilities=ModelCapabilities())

    kwargs = client.calls[0]
    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs
    assert "response_format" not in kwargs
    assert set(kwargs) == {"model", "messages"}


def test_consolidation_gate_off_request_carries_no_response_format():
    from attune.memory.mem0_store import Mem0Store

    class FakeMemory:
        def get_all(self, *, user_id, limit=100):
            return {"results": []}

        def add(self, *a, **kw):
            return {"results": []}

        def delete(self, *, memory_id):
            pass

    client = _FakeChatClient('{"promotions": [], "merges": [], "supersessions": []}')
    store = Mem0Store(memory=FakeMemory(), client=client)
    # No signals/facts at all short-circuits before any model call; feed one
    # signal so ``_consolidation_call`` actually runs.
    store._memory.items = {"a": {"id": "a", "memory": "x", "metadata": {"signal": "action"}, "user_id": "u1"}}  # type: ignore[attr-defined]

    def get_all(*, user_id, limit=100):
        return {"results": [store._memory.items["a"]]}

    store._memory.get_all = get_all  # type: ignore[method-assign]
    store.consolidate(user_id="u1")

    assert client.calls, "consolidation should have called the model"
    assert "response_format" not in client.calls[0]


# ---------------------------------------------------------------------------
# Structured-output parse-failure fail-closed (acceptance criterion 3).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    '{"priority": "NOT_A_REAL_PRIORITY", "reason": "x"}',
    '{"priority": "URGENT"}',
    "not json at all",
    "",
    None,
])
def test_triage_structured_parse_failure_falls_closed_to_routine(raw):
    result = _parse_triage_response(raw)
    assert result.priority == Priority.ROUTINE


def test_triage_structured_output_happy_path_parses_json_directly():
    result = _parse_triage_response('{"priority": "URGENT", "reason": "client escalation"}')
    assert result.priority == Priority.URGENT
    assert result.reason == "client escalation"


def test_consolidation_malformed_structured_response_mutates_nothing():
    from attune.memory.mem0_store import Mem0Store, _parse_consolidation_plan

    # Malformed JSON (structured output gone wrong) parses to None, same as
    # the pre-existing free-text-parse-failure path.
    assert _parse_consolidation_plan("{not valid json") is None
    assert _parse_consolidation_plan('{"promotions": "not-a-list"}') is None

    class FakeMemory:
        def get_all(self, *, user_id, limit=100):
            return {"results": [
                {"id": "a", "memory": "x", "metadata": {"signal": "action"}, "user_id": "u1"},
            ]}

        def add(self, *a, **kw):
            raise AssertionError("a malformed plan must never mutate the store")

        def delete(self, *, memory_id):
            raise AssertionError("a malformed plan must never mutate the store")

    client = _FakeChatClient("{not valid json")
    store = Mem0Store(memory=FakeMemory(), client=client)
    report = store.consolidate(user_id="u1")
    assert "no mutations applied" in " ".join(report.notes)


# ---------------------------------------------------------------------------
# prompt_version reaches the audit event and the ledger row (criterion 2).
# ---------------------------------------------------------------------------


def test_prompt_version_reaches_the_drafted_audit_event_and_ledger_row():
    langgraph = pytest.importorskip("langgraph")  # noqa: F841
    from attune.memory.base import MemoryRecord, MemoryStore
    from attune.orchestrator.draft_approve import build_draft_approve_graph

    class FakeStore(MemoryStore):
        def add(self, messages, *, user_id, metadata=None, infer=True):
            return []

        def search(self, query, *, user_id, limit=8, min_score=None):
            return [MemoryRecord(id="m1", text="prefers short replies", score=0.9)]

        def get_all(self, *, user_id, limit=100):
            return []

        def delete(self, memory_id):
            pass

    client = _FakeChatClient("Short reply, as you prefer.")
    graph = build_draft_approve_graph(client=client, store=FakeStore())
    state = {
        "user_id": "mumit", "domain": "mail", "action": "draft_reply",
        "incoming_ref": "msg-1", "incoming_summary": "Can we reschedule?",
        "audit_events": [], "iteration_count": 0,
    }
    result = graph.invoke(state, {"configurable": {"thread_id": "t-prompt-version"}})

    drafted = next(e for e in result["audit_events"] if e["event"] == "drafted")
    assert drafted["prompt_version"] == PROMPT_DRAFT.version

    ledger_rows: list[LedgerRow] = []

    class FakeLedger:
        def propose(self, row):
            ledger_rows.append(row)

        def complete(self, *a, **kw):
            pass

    record_proposal(
        FakeLedger(), thread_id="t-prompt-version", domain="mail", action="draft_reply",
        result=result, model_id="test-model",
    )
    assert ledger_rows[0].prompt_version == PROMPT_DRAFT.version


def test_prompt_version_reaches_brief():
    from datetime import datetime, timezone

    from attune.brief import assemble_brief
    from attune.connectors import McpWorkspaceConnector

    class FakeMcp:
        def __call__(self, server, tool, arguments):
            return {}

    client = _FakeChatClient("All quiet.")
    conn = McpWorkspaceConnector(FakeMcp())
    brief = assemble_brief(conn, client, now=datetime(2026, 7, 10, 7, tzinfo=timezone.utc))
    assert brief.prompt_version == PROMPT_BRIEF.version


# ---------------------------------------------------------------------------
# Native tool calling behind the capability probe (task 5).
# ---------------------------------------------------------------------------


def test_interaction_planner_uses_forced_tool_call_when_declared():
    from attune.interaction import plan_interaction, InteractionIntent

    class _ToolCall:
        class function:
            name = "emit_plan"
            arguments = json.dumps({
                "intent": "MAIL", "gmail_query": "is:unread from:sarah@example.com",
                "start": "NONE", "end": "NONE",
            })

    client = _FakeChatClient(reply=None, tool_calls=[_ToolCall()])
    plan = plan_interaction(
        client, "Did Sarah reply to my message?", timezone_name="UTC",
        capabilities=ModelCapabilities(supports_tools=True),
    )

    kwargs = client.calls[0]
    assert kwargs["tools"][0]["function"]["name"] == "emit_plan"
    assert kwargs["tool_choice"] == {"type": "function", "function": {"name": "emit_plan"}}
    assert plan.intent == InteractionIntent.MAIL
    assert plan.gmail_query == "is:unread from:sarah@example.com"


def test_interaction_planner_falls_back_to_text_when_tool_call_is_malformed():
    from attune.interaction import plan_interaction, InteractionIntent

    class _ToolCall:
        class function:
            name = "emit_plan"
            arguments = "not json"

    client = _FakeChatClient(reply=None, tool_calls=[_ToolCall()])
    plan = plan_interaction(
        client, "hello there", timezone_name="UTC",
        capabilities=ModelCapabilities(supports_tools=True),
    )
    # message.content is None and the tool call is malformed -> both parses
    # fail -> the deterministic fallback (never a crash).
    assert plan.intent == InteractionIntent.GENERAL


# ---------------------------------------------------------------------------
# Call hygiene: max_tokens/timeout/retry (task 6).
# ---------------------------------------------------------------------------


def test_call_kwargs_omit_unset_hygiene_knobs():
    from attune.llm import call_kwargs

    assert call_kwargs(ModelCapabilities()) == {}
    assert call_kwargs(ModelCapabilities(max_tokens=64, timeout_seconds=3.0)) == {
        "max_tokens": 64, "timeout": 3.0,
    }


def test_call_with_retry_retries_on_failure_and_stops_at_the_bound():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("transient")
        return "ok"

    caps = ModelCapabilities(max_retries=5, retry_base_delay=0.0)
    result = call_with_retry(flaky, capabilities=caps, sleep=lambda s: None, rand=lambda: 0.0)
    assert result == "ok"
    assert len(attempts) == 3


def test_call_with_retry_default_is_zero_retries_raises_immediately():
    calls = []

    def always_fails():
        calls.append(1)
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        call_with_retry(always_fails, capabilities=ModelCapabilities())
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Token usage and cache hit/miss visible in `attune metrics` (criterion 5).
# ---------------------------------------------------------------------------


def test_metrics_slice_aggregates_token_usage_and_cache_hit_rate():
    from datetime import datetime, timezone

    rows = [
        LedgerRow(
            proposal_id="p1", thread_id="p1", domain="mail", action="draft_reply",
            proposed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            input_tokens=100, output_tokens=50, cache_hit=True,
        ),
        LedgerRow(
            proposal_id="p2", thread_id="p2", domain="mail", action="draft_reply",
            proposed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            input_tokens=200, output_tokens=75, cache_hit=False,
        ),
        LedgerRow(
            proposal_id="p3", thread_id="p3", domain="mail", action="label",
            proposed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ),
    ]
    metrics = compute_metrics_slice(rows)
    assert metrics.total_input_tokens == 300
    assert metrics.total_output_tokens == 125
    assert metrics.cache_hit_rate == 0.5


def test_metrics_slice_tokens_and_cache_none_when_never_recorded():
    from datetime import datetime, timezone

    rows = [
        LedgerRow(
            proposal_id="p1", thread_id="p1", domain="mail", action="draft_reply",
            proposed_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ),
    ]
    metrics = compute_metrics_slice(rows)
    assert metrics.total_input_tokens is None
    assert metrics.total_output_tokens is None
    assert metrics.cache_hit_rate is None


def test_render_metrics_table_shows_token_and_cache_columns():
    from datetime import datetime, timezone

    now = datetime(2026, 7, 15, tzinfo=timezone.utc)
    rows = [
        LedgerRow(
            proposal_id="p1", thread_id="p1", domain="mail", action="draft_reply",
            proposed_at=now, input_tokens=1234, output_tokens=567, cache_hit=True,
        ),
    ]
    table = render_metrics_table(rows, now=now)
    assert "in_tok" in table
    assert "out_tok" in table
    assert "cache%" in table
    assert "1,234" in table
    assert "567" in table
    assert "100%" in table
