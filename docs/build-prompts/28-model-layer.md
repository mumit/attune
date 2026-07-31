# 28 — Model layer: prompt registry, caching, structured output, tool calling

**Phase P3** · `docs/plan-2026-h2.md` · **Depends on:** 24 · **Blocks:** 29, 33, 36

---

Read `CLAUDE.md` (the provider-neutrality boundary especially), the P3 section
of `docs/plan-2026-h2.md`, and `llm.py` — all 57 lines of it.

## Problem

`llm.py` builds an `OpenAI` client and calls `chat.completions.create`. Across
all of `src/attune`: **zero** `tools=`, **zero** `response_format`, **zero**
`cache_control`, **zero** streaming, no `max_tokens`, no `temperature`, no
timeout, no retry, and `response.usage` is never read. Structured output is
recovered by `json.loads` on free text; triage parses a two-line
`PRIORITY:`/`REASON:` contract out of prose.

`CLAUDE.md` requires provider neutrality through an OpenAI-compatible gateway.
That has been implemented as **lowest-common-denominator text-in/text-out**,
which is a capability floor, not neutrality. Neutrality means *degrading
gracefully* when a gateway lacks a feature — not declining the feature
everywhere. The cost is concrete: prompt caching is the single largest cost
lever available (cache hits bill at 0.10× standard input), and Attune cannot use
it because there is nowhere to declare a stable prefix.

Separately, the seven production prompts are inline string literals at their
call sites (`triage.py`, `draft_approve.py`, `brief.py`, `dispatcher.py`,
`mem0_store.py`, and two in `hosted/google_chat_conversation_executor.py`). No
version identifier, no registry. Nothing ties a recorded output to the prompt
that produced it — the `drafted` audit event records only the model name. Prompt
optimization (prompt 36) is impossible against string literals.

## Task

1. **A prompt registry.** `src/attune/prompts.py`: each prompt is a named,
   versioned object splitting **a stable prefix** (role, rules, output contract,
   canonical examples) from **a volatile suffix** (this thread, these memories,
   this playbook slice). Move all seven literals in, unchanged in content, and
   stamp `prompt_version` into every audit event and decision-ledger row that
   records a model output.

2. **Capability probing, not feature abandonment.** A small
   `ModelCapabilities` record per configured gateway — `supports_tools`,
   `supports_structured_output`, `supports_prompt_cache` — resolved from
   configuration (explicit settings, defaulting to off) rather than sniffed at
   runtime from an untrusted provider response. Every feature below is used when
   the capability is declared and falls back to today's exact behaviour when it
   is not. Same shape as `connectors/base.py`'s existing `supports_*()` probes.

3. **Prompt caching.** Mark the stable prefix cacheable when the gateway
   declares support. Note the economics before choosing a TTL: a longer TTL
   costs more on write and only pays back when the predictable cache lifetime
   exceeds it, and bursty traffic on a short TTL repeatedly pays write cost
   without recovering it. Report cache hit/miss in the ledger.

4. **Structured output where a contract already exists.** Triage's
   `PRIORITY`/`REASON` pair and the consolidation plan's JSON are already
   schemas pretending to be prose. Declare them properly when supported; keep
   the current text parse as the fallback path, and keep the fail-closed
   defaults (a parse failure still yields ROUTINE; a malformed consolidation
   plan still mutates nothing).

5. **Native tool calling behind the probe.** Do **not** convert the planner into
   an unrestricted tool loop — `docs/design.md` forbids that, and for good
   reason. Use tool calling only to replace text-parsed structured decisions
   (the planner's four-line `INTENT`/`GMAIL_QUERY`/`START`/`END` output is the
   obvious first case). The deterministic keyword fallback that currently
   overrides the model in three cases must survive unchanged; it is a safety
   control, not a workaround.

6. **Call hygiene.** `max_tokens`, per-task timeouts, and bounded retry with
   jitter — none exist locally today; `dispatcher._fetch_with_retry` is a bare
   3-shot loop with no delay. Read `response.usage` and record it, so the
   self-hosted runtime can meter what the hosted plane already meters in
   `model_usage_daily`.

7. **Restore the `DRAFT` task to the hosted gateway.** `hosted/model_gateway.py`
   allows exactly `{"classify", "converse", "embed"}`. That omission is the
   literal reason the hosted plane cannot draft, which is in turn why its one
   registered capability is dormant. Add `draft` with the same bounded-envelope
   discipline the other tasks have.

## Constraints

- **No provider-specific client, ever.** Everything here goes through the
  official `openai` SDK surface against a configured base URL. If a feature
  cannot be expressed that way, it does not ship.
- Gate-off must be **byte-identical** to today's request shape. Follow the
  precedent in `docs/decisions.md`'s tenant-model-profiles entry: build kwargs
  conditionally, never pass a literal `None`, so unmodified fakes receive the
  exact pre-existing call. Zero churn in existing tests is the bar.
- The model remains untrusted. Structured output makes a proposal
  better-typed; it does not make it authoritative. Trusted code still binds
  actor and tenant, validates the capability, and enforces the rung.

## Acceptance

- A test asserting every capability-off path produces a request dict identical
  to the one recorded before this change.
- A test asserting `prompt_version` reaches the audit event and the ledger row
  for triage, draft, and brief.
- A test asserting a structured-output parse failure still yields the fail-closed
  default.
- A test asserting the hosted gateway accepts `draft` with its bounded limits
  and rejects an unknown task.
- Token usage and cache hit/miss visible in `attune metrics`.
- `docs/decisions.md` entry recording the capability-probe design and the
  explicit reading of "provider-neutral" as *graceful degradation*, not
  lowest-common-denominator.
