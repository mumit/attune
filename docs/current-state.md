# Current state — full product review (2026-07-18)

This is a point-in-time review of Attune across architecture, design,
implementation, security, and user experience, covering both the
single-principal self-hosted runtime and the hosted multi-tenant platform.
It is descriptive, not normative: [`design.md`](design.md) and
[`security-architecture.md`](security-architecture.md) remain the design
authorities. The companion documents are the
[gap analysis](gap-analysis.md) against the product goal and the
[future-state plan](future-state.md).

## Scale snapshot

- ~24,900 LOC single-principal runtime across 147 files (`src/attune/`,
  excluding `hosted/`); ~17,700 LOC hosted platform (`src/attune/hosted/`,
  98 files); ~5,700 lines of Terraform (`deploy/gcp/`).
- 112 test files, ~1,035 test functions; 41 hosted SQL migrations; 27 docs.
- Local configuration surface: 49 variables in `.env.example`. Hosted
  operator surface: ~95 additional `ATTUNE_` variables plus 12+ default-off
  activation gates.

## Two products in one repository

Attune is currently two parallel systems that share a repository, a design
philosophy, and Workspace connectors — but no intelligence code:

1. **The single-principal runtime** — SQLite/JSONL/Mem0+Qdrant, LangGraph
   draft-approve workflows, the earned-autonomy ladder, briefs, follow-up
   nudges, and conflict holds. This is where all product intelligence lives.
2. **The hosted multi-tenant platform** — Cloud Run services over forced-RLS
   PostgreSQL with envelope-encrypted credentials, broker-mediated provider
   access, and a hash-chained audit spine. This is where all tenant-grade
   security engineering lives. Its product surface today is bounded
   read-only conversation over three front doors (Google Chat, Slack,
   browser) plus onboarding, channel lifecycle, retention, and export
   ceremonies.

`grep` confirms the hosted package imports nothing from
`attune.orchestrator`, `attune.memory`, `attune.brief`, or
`attune.conversation`. The hosted conversation executor
(`hosted/google_chat_conversation_executor.py`, subclassed by the Slack and
web executors) reimplements routing, bounded reads, and response generation
from scratch. Every future intelligence improvement currently has to be
built twice or deliberately converged.

## Architecture — single-principal runtime

```text
Gmail/Calendar/Chat/Slack events
  ├─ push: Pub/Sub pull loops (runtime.py)
  └─ poll: single timer (runtime.py)
        ▼
ingestion/*  — cursor reconciliation (historyId, syncToken, high-water mark)
        ▼
dispatcher.py — the one routing seam (event → triage → graph → channel)
        ▼
orchestrator/draft_approve.py — LangGraph graph, SqliteSaver-checkpointed
  retrieve → draft → gate → approve(interrupt) → apply → capture
        ▼
channels/ (Slack Socket Mode, Google Chat cards) · audit/log.py (JSONL)
```

Durability discipline is the standout implementation strength:

- Cursor-then-retry semantics: fetch failures after a cursor advance enqueue
  to `SqliteRetryQueue` rather than dropping items (`dispatcher.py`).
- `HistoryExpired`/`SyncExpired` rebaseline without dispatching, avoiding
  card floods for pre-existing state.
- `SourceChangedError` freshness checks re-verify the live thread/event
  immediately before any write, closing the stale-approval class.
- Atomic temp-file + `os.replace` writes for grants, pending approvals, and
  retry state; poison Pub/Sub messages are acked and audited, not re-looped.
- `PendingApprovals.claim()` gives single-resolution semantics against
  double-click races (in-process only; see security findings).

Known implementation weaknesses: `dispatcher.py` (~1,310 lines) and
`runtime.py` (~1,276 lines) carry heavy branching with near-duplicate
channel-dispatch conditionals; broad `except Exception` is a deliberate
per-source-isolation policy but swallows programming bugs identically to
network blips; memory-UI confirmation state is process-local and lost on
restart; LangGraph state travels as `dict[str, Any]` enforced by convention.

Test depth is very good on safety-critical paths (77 dispatcher tests;
memory-consolidation mechanics have a LoCoMo-style regression harness;
23 graduation-suggestion tests). Gaps: real Mem0/Qdrant retrieval quality is
only checked behind a manual `ATTUNE_LIVE_MEMORY_EVAL=1` gate, never CI; the
live loop wiring in `Runtime.run` is `pragma: no cover`; and no test
exercises whether repeated ignore signals actually shift future triage.

## Intelligence layer — what "learns" and "suggests" mean today

This is the area furthest from the product goal; the
[gap analysis](gap-analysis.md) quantifies it. Facts as implemented:

- **Triage** is one `Task.CLASSIFY` model call per incoming Gmail thread
  returning URGENT/ROUTINE/NOISE (`orchestrator/triage.py`). Only the NOISE
  branch is consumed — URGENT and ROUTINE are treated identically
  downstream. Calendar events and Chat/Slack messages have no triage at all.
- **Learned importance** is a soft overlay: up to three memory hits for
  "reactions to mail from {sender}" are injected into the classify prompt.
  There is no per-sender/per-topic importance profile, no deterministic
  demotion rule, and no scoring that compounds.
- **Memory** writes at three points: correction capture on edited-then-
  approved drafts (diff-based, Mem0 `infer=True`), raw approve/edit/reject/
  ignore action signals, and explicit "remember X" teaching. Reads occur in
  four places: the draft `retrieve` node, triage past-reactions, brief
  meeting prep, and conversational fallback. So memory genuinely shapes
  drafts and prep — but day-to-day behavior reads raw signals; actual
  crystallization happens only in the nightly `consolidate()` pass, which
  requires 3+ repeated signals and aborts conservatively.
- **Proactive surface** is exactly: the morning brief (`brief.py`),
  follow-up nudges for quiet sent-threads (max 3/run, 7-day cooldown),
  same-day conflict hold offers (max 3/run), and a weekly informational
  autonomy digest. Caps are count-per-run in arrival order, not
  importance-ranked.
- **Autonomy ladder** is mechanically solid: `(Action, Domain) → Rung`
  matrix, live-reloaded on every gate evaluation, with evidence-based
  graduation suggestions computed from the audit log (≥10 decisions,
  0 rejections, ≥95% unedited approvals). But graduation is 100% manual —
  only `attune autonomy grant` (CLI) can accept a suggestion; chat is
  show-only. Nothing in the default matrix exceeds PROPOSE. `SEND_REPLY`
  stays structurally inert even when granted (requires separate
  `send_enabled` plus a `gmail.send` scope). `DECLINE_INVITE`, `RESCHEDULE`,
  and `LABEL` exist in the enum vocabulary with zero implementing call
  sites.

## Architecture — hosted multi-tenant platform

Services (each its own Cloud Run service and workload identity): control
plane (all owner-facing HTTP), worker, dispatch broker (opaque-intent task
authority), secret broker (KMS-wrapped credential vault), channel broker(s)
(Slack + Google Chat lifecycle and delivery), per-provider ingress services,
model gateway (two fixed tasks: classify, converse), export pipeline,
retention job, OAuth exchange/callback pair, audit writer, migrator.

Tenancy enforcement is the strongest engineering in the repository:

- `FORCE ROW LEVEL SECURITY` on every tenant table;
  `attune.current_tenant_id()` raises on unset context (fail-closed);
  tenant context is transaction-local so pooled connections cannot leak it.
- Privileged transitions are `SECURITY DEFINER` functions owned by
  memberless `NOLOGIN` roles; runtime roles hold no `BYPASSRLS`.
- Idempotency and replay protection are structural: unique
  `(tenant_id, idempotency_key)` jobs with exact-match re-reads,
  deterministic Cloud Task names, one-use hashed link codes and OAuth state,
  and an append-only, per-tenant hash-chained audit table with
  update/delete/truncate triggers that unconditionally raise.
- The roadmap's claims verify against code — including a live-found
  reinstall defect fixed by migration 0039, evidence the ceremonies are
  genuinely exercised.

Maturity honestly stated: implemented **and live in development** — identity
and sessions, Workspace connect/verify/disconnect, the R0 policy ceremony,
Slack and Google Chat installation/test/disconnect lifecycles, all three
conversation front doors, protocol retention with paging, and most export
slices. Implemented **but dormant** — the typed capability gateway (tested;
imported by no live path) and the `PostgresMemoryRepository` (schema and
pgvector search exist; no executor calls it). **Missing** — production
signup (first sign-in dead-ends on operator provisioning), customer-content
retention/deletion, export completion/download/UI, billing and quotas,
per-tenant model configuration, support/repair tooling, SLO-grade
monitoring (seven job-failure alert policies only), and production scale
(every service capped at `max_instance_count = 3`).

## Security posture

The full review confirmed the documented model is substantively implemented
where the docs claim it is: no prompt-injection-to-write path, no SQL
injection, no missing webhook verification, and no cross-tenant RLS bypass
were found. Untrusted content is consistently provenance-framed in every
LLM call; write intent from free-form chat is deterministically refused;
the only two write paths (Gmail draft creation, calendar hold) sit behind
the approval interrupt or an explicit grant.

Findings, prioritized (all in the local runtime unless noted):

| # | Sev | Finding |
|---|-----|---------|
| F1 | Med | Local JSONL audit log has no hash chain or tamper evidence, yet is the trust root for autonomy-graduation suggestions (`audit/log.py`, `orchestrator/grants.py`). |
| F2 | Low/Med | `JsonPendingApprovals.claim()` is guarded only by an in-process lock; two overlapping runtime processes could double-claim one approval (`orchestrator/pending.py`). |
| F3 | Low | Secret redaction in logs is a stated writing discipline, not a filter (`logging_setup.py`). |
| F4 | Low | Republisher Chat OIDC verification is self-documented as never exercised against a live Chat app (`deploy/republisher/main.py`). |
| F5 | Low | With `ATTUNE_DATA_DIR` unset, state files (conversation text, audit) fall back to CWD with default umask (`config/__init__.py`). |
| F6 | Info | Correction-derived memories (which touched untrusted content) are not provenance-weighted differently from explicit teaching at retrieval time — a theoretical two-stage memory-poisoning path. |
| F7 | Info | Hosted vector retrieval (SEC-201) is unimplemented; tenant filtering must be adapter-injected the moment it lands. |
| F8 | Low | `GoogleChatChannel.handle_interaction` has no in-class actor guard (authorization lives one layer up in the dispatcher), unlike `SlackChannel`. |
| F9 | Info | No local rate/volume ceilings on model calls or channel messages; hosted has Cloud Armor and DB-level leasing, local has nothing analogous. |

## User experience

**Self-hosted.** Time-to-first-value is dominated by manual Google Cloud
Console work (project, API enablement, consent screen, Desktop OAuth
client) that the CLI cannot see or assist with; a silent second trap is the
7-day refresh-token expiry of External/Testing OAuth apps, which Doctor
does not warn about. The `attune init` wizard front-loads ~25 questions.
Once past setup, the operational UX is genuinely good: Doctor is a strong
diagnostic surface, init is idempotent and line-preserving, status/repair
are secret-free and ownership-safe, and memory/autonomy have clean CLI
loops. Walls: free-form mutation is always refused by design; a granted
`send_reply` prints a warning but remains inert; Pub/Sub mode is a steep
second deployment track.

**Hosted.** A minimum of seven distinct ceremonies (sign-in, Workspace
consent, verification, policy, channel preference, channel install,
delivery test) before any non-browser conversation, several gated by a
ten-minute recent-authentication window that bounces a distracted user to
re-login. Google Chat linking requires typing `/link CODE` into a DM. The
web panel is poll-only (2s) with no push or email fallback. Bounded reads
(10 Gmail thread summaries, 25 events/7 days) are fixed and unexplained in
the UI. Only owner DMs are supported — no shared spaces. And there is no
production signup: a new identity dead-ends until an operator provisions
membership. All of this is consistent with the docs' own framing —
a development activation, not an operated product.

## Overall assessment

Attune is an unusually disciplined codebase with honest documentation: the
safety spine (approvals, durability, audit, tenancy) is largely built and
verified, and the hosted platform's isolation engineering exceeds what most
products ship with. What is thin is the product's stated reason to exist:
importance is computed once and mostly discarded, learning crystallizes too
slowly to feel adaptive, the suggestion surface is two features, autonomy
never graduates without a terminal command, and none of the intelligence
exists in the hosted path at all. The [gap analysis](gap-analysis.md) maps
these against the goal; the [future-state plan](future-state.md) sequences
the work.
