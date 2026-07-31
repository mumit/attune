# Landscape review — Attune against the personal-agent market (2026-07-31)

A point-in-time competitive review. Companion to the [2026-07-18 review
trilogy](current-state.md); where that trilogy scored Attune against its own
product goal, this one scores it against what shipped elsewhere. The
remediation sequence is in [plan-2026-h2.md](plan-2026-h2.md).

Source-quality note: OpenAI's own domains return HTTP 403 to automated
fetching, so every OpenAI claim below is secondary-sourced. Several vendor
prices resolved in CAD/EUR. Numbers marked *(vendor)* are self-reported and
not independently replicated. Where sources conflicted irreconcilably, no
number is quoted.

## 1. The market's own verdict on the product Attune is

2026 was a culling. The pattern matters more than any individual death.

| Product | Fate | What it cost |
|---|---|---|
| **ChatGPT Pulse** — proactive daily brief from mail, calendar, history | Launched 2025-09-25, **retired 2026-06-17** | Nine months, $200/mo tier only |
| **ChatGPT agent mode** / Operator | Both retired; help page reads "no longer available" | Operator's standalone surface died 2025-08-31 |
| **ChatGPT Atlas** browser | Launched 2025-10-21, **dies 2026-08-09** | Under ten months, never left macOS |
| **Google Project Mariner** | **Shut down 2026-05-04** | ~17 months |
| **Clockwise** — the calendar-AI-for-teams company | Salesforce acqui-hired the team; **product dead 2026-03-27, roughly one week later**; data deleted | **$76M raised**, incl. a $45M Series C |
| **Katch** — agentic scheduling | `gokatch.ai` **fails DNS resolution today** | $4M+ seed, no shutdown notice |
| **Humane AI Pin** | **Bricked 2025-02-28**, ten days after HP bought the assets for $116M; all customer data deleted | >$230M raised, **~$9M lifetime revenue** |
| **Rabbit r1** | ~**5,000 DAU on 100,000+ units sold**; staff unpaid from July 2025; by rabbitOS 2.3 the headline feature is *running Claude Code* | The "Large Action Model" was Playwright scripts |
| **Limitless Pendant / Rewind** | Meta acquired 2025-12-05, **Pendant sales halted immediately**; Rewind capture **disabled 2025-12-19** | Both independent pendant firms are now inside big tech (Amazon bought Bee) |
| **OpenAI × Jony Ive device** | **Court filings say not before end of Feb 2027**; lost the "io" name to an injunction | $6.5B all-stock |

Two readings of this, and both are load-bearing for Attune.

**The one that hurts.** Attune's flagship proactive surface — an ambient daily
brief assembled from mail and calendar — is precisely the product OpenAI built,
measured for nine months at its highest price tier, and killed. Its stated
reason is the finding, not the spin: proactive experiences work when they are
*"personalised, action-oriented, and steerable by the user,"* and engagement
concentrated in **tasks** inside the Pulse surface rather than in the brief
itself. Two other vendors independently landed in the same place — Google
shipped **Scheduled Actions** (2026-05-19) with *"daily calendar summaries"* as
its worked example, and Anthropic shipped **scheduled tasks inside Cowork**
(Feb 2026). Three of three converged on **user-authored, steerable recurring
tasks** and away from an ambient push nobody asked for.

The failure mode is quantified. Users tolerate roughly **three to five
unsolicited AI updates per day across all sources combined**; past that they
mute, then uninstall. Attune's proactive caps are count-per-run in *arrival
order* — `MAX_NUDGES_PER_RUN=3`, `MAX_HOLD_OFFERS_PER_RUN=3`,
`MAX_LABEL_PROPOSALS_PER_RUN=3`, `MAX_DECLINE_PROPOSALS_PER_RUN=2` — which can
spend the entire daily budget on whatever happened to arrive first, plus a
brief with no ranking of its own. The same failure shows up in the market as
complaints: Superhuman Go's proactivity reads as *"intrusive"*, Motion's
autonomous rescheduling ("reschedules your entire day when one thing changes
without letting you approve") is its single loudest complaint, and Cora
time-shifts messages mid-conversation so users reply late or miss replies
entirely. Cora's retention is reported as **bimodal — users quit within days or
use it several times daily.**

**The one that helps.** Every product in that table took its users' context
with it when it died. Clockwise deleted the optimization graph rather than
migrating it. Humane bricked devices ten days after the announcement and
refunded only buyers inside a 90-day window. Rewind switched off capture. A
self-hosted, single-principal assistant with a local audit log, an inspectable
memory store, and no dependency on a vendor continuing to operate is the one
shape in this category that structurally cannot do that to its user. **That is
Attune's actual moat, and the product does not currently say so anywhere.**

And Attune cannot win the fight it is not in. Gmail's thread summaries, Help Me
Write, and Suggested Replies became **free to every Gmail user** in January
2026 — that set is the entire paid product at several startups. **Personal
Intelligence** (Gemini reasoning across Gmail, Photos, YouTube, Search history,
and Docs) went **free for all US users 2026-03-17**. **Alexa+** is $19.99/mo
standalone but **free with Prime**, i.e. free to ~200M people. The winning
consumer model is not "assistant subscription," it is "assistant as retention
feature of a subscription you already have." Attune has no path there and
should stop implying one.

## 2. Capability comparison

Attune's complete write surface is six actions: create a Gmail draft reply,
send a reply, archive to one hardcoded `Attune/Noise` label, create a
*tentative* hold with `attendees=[]` forced, decline an invite, reschedule its
own event. Slack and Google Chat have **zero** write capability despite holding
grants in the permission matrix.

| Capability | Attune | Claude connectors | Outlook Agent Mode (preview) | Gemini / Workspace Intelligence |
|---|---|---|---|---|
| Draft a reply | ✅ | ✅ | ✅ | ✅ |
| **Send** mail | flag-gated, off by default, MCP backend refuses | **❌ on every tier** — "all emails must be sent manually" | ✅ | ✅ |
| Send a *new* (non-reply) mail | ❌ | ❌ | ✅ | ✅ |
| Reply-all / CC / BCC / attachments | ❌ (bare `MIMEText`, single recipient) | partial | ✅ | ✅ |
| Arbitrary labels, move, mark-read, snooze, trash | ❌ (one label, archive only) | partial | ✅ safe-archives non-critical | ✅ Skills auto-label |
| Prioritized inbox view | brief only | ❌ | ✅ dynamic stakeholder rules | ✅ **AI Inbox** rebuilt around to-dos + VIPs |
| Unresolved-thread follow-ups | ✅ nudges (3/run, 7-day cooldown) | ❌ | ✅ after 24h | ✅ |
| Create event **with attendees** | ❌ (`attendees=[]` forced) | ✅ | ✅ | ✅ |
| **Find mutual availability** across attendees | ❌ (`primary` only, hard 08:00–18:00) | ✅ | ✅ | ✅ (same-tenant free/busy) |
| RSVP **accept** / tentative | ❌ (decline only) | ✅ | ✅ accept/decline/delegate | ✅ |
| Delete / cancel event | ❌ | ✅ | ✅ | ✅ |
| Recurring-event awareness | ❌ (`singleEvents=True` flattens) | ✅ | ✅ | ✅ |
| Resolve double-bookings | detects, offers a hold | — | ✅ reschedules | ✅ |
| **External** scheduling negotiation | ❌ (explicitly deferred) | ❌ | ❌ | ❌ — "Help me schedule" inserts times the recipient clicks; cannot counter-propose |
| Act in Slack / Chat | ❌ | via MCP | — | ✅ **Ask Gemini in Chat is GA** |
| Tasks / to-dos | ❌ | ✅ | ✅ | ✅ |
| Drive / Docs / attachments | ❌ | ✅ read/write | ✅ | ✅ |
| Meeting capture | ❌ | — | ✅ | ✅ in-person + Zoom + Teams |
| **Undo** | ❌ *(`grep -rn undo src/` → 0 matches)* | — | ✅ override at any stage | — |
| Batch approval | ❌ (one card, one action) | — | ✅ | — |
| Computer-use fallback | ❌ | ✅ Chrome GA, all paid plans | — | ✅ Spark drives Chrome with saved passwords |
| Event-driven triggers | ✅ **Pub/Sub push** | ❌ "tasks run on a schedule, not when mail arrives" | — | ❌ scheduled only |
| Voice | ❌ | — | — | ✅ |
| Self-hostable, no vendor dependency | ✅ | ❌ | ❌ | ❌ |
| Tamper-evident local audit of every effect | ✅ | ❌ | tenant audit | ✅ agent audit |
| Earned, revocable per-action autonomy ladder | ✅ | ❌ | ❌ | ❌ |

Three observations that are easy to miss.

**Attune's event-driven ingestion is a real, uncommon advantage.** ChatGPT Work
is described as *"an interactive copilot rather than an autonomous background
assistant"* — hourly polling, no webhooks, *"nothing happens unless you start
it."* Claude's consumer connectors: *"Tasks run on a schedule, not when mail
arrives."* The only genuinely event-triggered offerings anywhere are
developer-tier (Claude Code Routines, Zapier Agents). Attune's Gmail/Chat
Workspace Events → Pub/Sub path is ahead of both consumer leaders. Nothing in
the product markets it.

**Restraint is a marketed feature now, and Attune should claim it.** Cora
advertises that it *structurally cannot* send or delete. Claude cannot send
email on any tier — a deliberate asymmetry against its full read/write
Calendar. Microsoft leads with override-at-any-stage and a DLP control that
stops Copilot grounding answers in externally-received mail. **Skej — the only
shipping product that genuinely negotiates with an external human over email —
gates "Unsupervised Mode" behind its $23/mo premium tier.** The vendor closest
to real autonomy prices *not* needing supervision as the upsell. That is
Attune's autonomy ladder as a business model.

**Nobody does external scheduling negotiation, and the ones who tried died.**
Skej ships it. Katch died attempting it. Cal.com's email channel is still
"coming soon" despite its blog claiming otherwise. Every large vendor collapses
to same-tenant free/busy or a booking link. This is genuinely open ground — and
genuinely hard, which is why `scheduling.py` deferring it was defensible.

## 3. What became table stakes in mid-2026

Ordered by how much Attune's absence costs.

1. **MCP as a server, not only a client.** MCP was donated to the Agentic AI
   Foundation under the Linux Foundation in December 2025 (co-founded by
   Anthropic, Block, and OpenAI; AWS, Google, Microsoft, Cloudflare, Bloomberg
   as platinum members), ~97M monthly SDK downloads. **Slack, Calendly,
   Cal.com, Granola, Glean, and Google Workspace all now expose MCP servers**
   so other people's agents can call them. Slackbot became an MCP client into
   6,000+ apps. Dropbox shipped Reclaim as an app *inside* ChatGPT. Attune
   consumes a versioned six-tool contract and exposes nothing — so its
   importance profile, triage, and brief are unreachable by any other agent.
2. **A migration plan for the 2026-07-28 MCP spec, which is a breaking
   rewrite.** Maintainers call it the largest change since authorization: the
   core **goes stateless** — the `initialize`/`initialized` handshake and the
   `Mcp-Session-Id` header are eliminated, client info moves into per-request
   `_meta` — and **Roots, Sampling, and Logging are deprecated** (functional
   through ~May 2027 under a new 12-month lifecycle policy). A new extensions
   framework adds **MCP Apps** and **Tasks** (async long-running operations
   with polling, mid-flight input, durable handles). `docs/mcp-contract.md`
   v1.1 predates all of it, and the Streamable HTTP session assumptions in
   `connectors/mcp_client.py` are exactly what changed. The **Tasks** extension
   is a near-exact match for draft-approve interrupt semantics.
3. **Evals and observability as a product surface.** Now commoditized,
   therefore expected: LangSmith, Langfuse (MIT, self-hostable), Braintrust
   (CI regression gates posting per-scorer deltas as PR comments), Arize
   Phoenix, W&B Weave — nearly all with a free or OSS tier. Microsoft shipped
   an M365 Copilot Agent Evaluations tool in preview; n8n shipped Evaluations
   with per-test-case status. **LangChain's open-source reference email
   assistant ships LLM-as-judge evals for email responses, tool-call accuracy,
   and triage decisions.** That is Attune's architecture published as a
   tutorial, with the eval suite Attune lacks.
4. **Sub-agent delegation with clean context windows.** Claude Code
   orchestrates *"dozens to hundreds of subagents"*, sub-agents spawn
   sub-agents up to five levels, background subagents by default. LangChain's
   reference personal assistant is a supervisor with dedicated Calendar and
   Email subagents plus `HumanInTheLoopMiddleware`. Anthropic's own context
   guidance names sub-agents returning **1,000–2,000-token condensed
   summaries** as one of four core techniques. Attune has none — and
   `dispatcher.py` is 2,478 lines.
5. **Reversibility and batch review as approval-fatigue controls.**
   LangChain's **Agent Inbox** is the reference UX and defines the vocabulary:
   an inbox with **accept / edit / respond / ignore** per interrupt, and three
   HITL patterns — **notify, question, review**. The named counter-risk: *"the
   control breaks when approval stops being a real decision and becomes a
   reflex."* Attune has three buttons per card, no batching, no undo, and no
   expiry.
6. **Prompt-injection defense as a named, measured control.** Google publishes
   a six-layer strategy (injection classifiers, security thought reinforcement,
   markdown sanitization and URL redaction via Safe Browsing, a
   user-confirmation framework, end-user notifications, adversarial
   robustness). **Anthropic publishes its numbers: injection succeeded 23.6%
   without mitigations, 11.2% with them** — roughly 1 in 9 even when protected.
   The attack surface is email-specific and real: **ZombieAgent** against
   ChatGPT's Gmail connector was zero-click, server-side, persistent, and
   self-propagating; **GeminiJack** achieved zero-click exfiltration from
   merely sharing a Doc or a calendar invite. Attune's controls here are
   genuinely good — `trusted_context` out-of-band provider facts,
   `[UNTRUSTED mail]` fencing, `frame_memory_text` provenance weighting, no
   write path on the source-message route — and completely unmeasured. There is
   no injection eval anywhere in the repo.
7. **Metered pricing, and the bill-shock problem it creates.** ChatGPT Work is
   metered with no published per-task rates. Motion, Cal.com, Microsoft,
   Notion, Manus, Dust, and Zapier all meter credits on top of seats. Notion
   users report **$150–200 against a $20 expectation**; Manus can drain a month
   of credits in minutes with no runaway alert; Lindy responded by hiding
   credit counts entirely; Zapier agents simply **stop functioning** at the
   cap. Attune's hosted plane has per-tenant usage metering already
   (`model_usage_daily`) and no local equivalent — the self-hosted runtime
   never reads `response.usage` at all.
8. **Computer-use / browser fallback.** Claude in Chrome GA on all paid plans
   with record-and-replay; Claude Code computer use in CLI and Desktop; Gemini
   Spark drives Chrome using saved passwords. Attune's four sources all have
   good APIs, so this is not urgent — and the counter-argument is strong (11.2%
   residual injection success; Atlas's deprecation cited injection and
   URL-handling leaks). For a product whose stated principle is *"the model is
   not a security principal,"* staying out of the browser is probably correct.
   **What's missing is the written decision, not the feature.**
9. **Voice, mobile, ambient meeting capture.** Production voice targets are
   p50 under 250–400ms over WebRTC. Otter crossed $100M ARR with an agent that
   speaks in meetings and an SDR agent that books them; Granola raised **$125M
   at $1.5B (2026-03-25)** explicitly to be an *"enterprise AI context layer"*
   with an MCP server. Meeting content is a primary importance signal Attune
   cannot see. **But**: Otter's class action over recording without
   all-participant consent **survived a standing challenge in early 2026**,
   with a federal court finding non-consensual recording a concrete injury.
   Consuming Granola/Circleback **over MCP** is materially lower-risk than
   building capture.
10. **A2A as the second protocol layer.** Linux Foundation, Apache-2.0, 100+
    companies. Agents publish an `AgentCard` at
    `/.well-known/agent-card.json`; JSON-RPC 2.0 with an eight-state task
    lifecycle including `input_required` and `auth_required`. **MCP + A2A has
    crystallized as the two-layer reference model.** `input_required` is
    Attune's approval interrupt, named by someone else's standard.

## 4. Benchmarks — there is no scoreboard for this product

The clearest finding of the research: **there is no maintained, leaderboard-backed
benchmark for email triage, email prioritization, or reply-draft quality.** No
"EmailBench" exists. Enron/Avocado are now used almost exclusively for
privacy/leakage evaluation. Reply-draft quality has no benchmark at all.

What does exist, and what it says:

- **ClawsBench** (2026-04) is the closest citable artifact — 44 tasks across
  five high-fidelity mock services **including Gmail, Calendar, and Slack**,
  deterministic snapshot/restore. **Top-5 models cluster at 53–63% success with
  7–23% unsafe-action rates** (overall range 7–33%), across eight recurring
  unsafe patterns including multi-step sandbox escalation and silent contract
  modification. Its conclusion — **capability gains do not track safety** — is
  the strongest external justification for Attune's approval spine that exists.
- **π-Bench**: task completion and **proactivity are distinct capabilities**.
  Attune optimizes neither explicitly.
- **CalBench**: multi-agent scheduling under private calendars, scoring privacy
  leakage and burden fairness. Finding: **privacy preservation paradoxically
  harms fair burden-sharing.**
- **GroupMemBench** (Microsoft Research, 2026-05) tests memory in
  **multi-party conversations — channels and groups** — arguing existing memory
  benchmarks are all single-user and therefore mis-specified. That is Attune's
  attended-sources work exactly.
- **GateMem** (2026-06) is the only benchmark testing **memory governance and
  agent-facing active forgetting after deletion requests** — directly relevant
  to `attune memory forget` and the hosted erase path.
- **PrefEval** (ICLR 2025 oral): **zero-shot preference-following accuracy
  falls below 10% at just 10 turns (~3k tokens)** across most of 10 models.
  Soft, prompt-injected preference memory is the *known-failing* approach.
- **StreamMemBench** (2026-06): systems store evidence but **fail to convert
  feedback into reliable follow-up.** That is a one-line description of
  Attune's current learning loop.
- **HAL** (Princeton, cost-controlled, the most credible third-party evaluator)
  **paused updating accuracy leaderboards to measure reliability instead**,
  reporting that *"all new models make a noticeable jump in accuracy, but their
  reliability is not improving at nearly the same rate."*
- **Vending-Bench 2** is the best public proxy for year-scale coherence: a
  strong human strategy earns ~$63,000; **Claude Opus 4.6 topped at
  $8,017.59** — about 13% of human performance. The most sobering honest number
  in the research.

**Two cautions before anyone quotes a memory score.** A Penfield Labs audit
(2026-04-08) found **6.4% of LoCoMo's answer key is wrong** (99 score-corrupting
errors in 1,540 questions, ceiling ~93.6% not 100%) and that **the standard LLM
judge accepts 62.81% of deliberately wrong-but-topically-adjacent answers** —
so the optimal LoCoMo strategy is context-stuffing plus verbose on-topic
waffle, and reported "100% LoCoMo" runs used `top_k=50`, effectively retrieving
the whole conversation. Separately, Zep reproduces Mem0's reported Zep score of
65.99% as **75.14%** after fixing three configuration errors, and notes LoCoMo
conversations are only **16–26K tokens** — inside every modern context window,
where a **full-context baseline (~73%) beats Mem0's best (~68%)**. **There is
no neutral, maintained memory leaderboard as of July 2026.**

Consequence for the roadmap: since no external benchmark covers this surface,
**a defensible internal eval suite is a differentiator here, not table stakes**
— but only if it runs automatically. Today Attune's live memory eval is gated
behind a manual flag *and cannot execute at all* (the workflow never sets
`ATTUNE_EMBEDDING_DIMENSIONS`, which `build_mem0_config` raises without).

## 5. Where the learning thesis has moved

Attune's premise — an assistant that compounds — is right, and the technique
set changed under it.

**A filesystem the model edits beats a retrieval index the model queries.**
Three independent 2026 results:

- **LongMemEval-V2** (451 curated questions over real WebArena/WorkArena
  trajectories, 25M–115M tokens): a **coding agent over file-based memory
  scores 74.9%**; the RAG-based variant of the same system **58.6%**; simple
  RAG **42.8%**. Abilities measured: static state recall, dynamic state
  tracking, **workflow knowledge, environment gotchas, premise awareness** —
  i.e. exactly what an assistant learns about its principal.
- **Letta abandoned memory blocks in February 2026** for git-backed Markdown
  "Context Repositories": every memory change auto-committed with a message,
  subagents holding divergent branches and merging.
- **Anthropic's memory tool is GA** (`memory_20250818`, no beta header, Claude
  4+) and is *file operations* — `view`/`create`/`str_replace`/`insert`/
  `delete`/`rename` — not retrieval. With context editing it reported **84%
  token savings and 39% performance improvement** on a 100-turn task.

**Reflective text optimization beats RL by two orders of magnitude in data.**
**GEPA** (ICLR 2026 oral) beats GRPO by ~10% average, up to 20%, **with up to
35× fewer rollouts**, matching GRPO's best validation score in 306–1,179
rollouts — **up to 78× sample efficiency**. A single principal produces that
much signal in a month or two. **ACE** (evolving playbooks, incremental *delta*
edits specifically to avoid brevity bias and context collapse) gets **+10.6% on
agents and +8.6% on finance with no labels at all**, from natural execution
feedback. A production case study of accumulated behavioral rules in a
version-controlled instruction file recorded **zero recurrences across 9 error
classes over 74 post-rule exposures**, with rules originating *only* from
accepted human review comments.

**Attune's nightly consolidation now has a citation.** Sleep-time compute:
**~5× less test-time compute for equal accuracy, +13–18% accuracy from scaling
it, 2.5× lower cost per query** amortized — with benefit proportional to
**query predictability**. A personal assistant's mornings are the most
predictable workload there is.

**Context rot makes Attune's current retrieval actively harmful, not merely
weak.** Chroma's study across 18 frontier models found degradation is
**continuous, not a cliff** (a 200K-window model can degrade at 50K), and that
**distractors — semantically similar but irrelevant content — degrade beyond
what length alone explains.** Attune retrieves a fixed `k=8` using the *entire
email body* as the query, with `min_score` present in the interface and unused
by every production caller. That is a distractor generator.

**Two things to stop planning.** *(a)* The roadmap's "optional Graphiti
migration path" should be dropped on current evidence — MemDelta shows that
once hidden confounds are controlled, the Mem0/Graphiti/MemGPT advantage over
properly-built simple baselines largely evaporates; Mem0 now ships multi-signal
retrieval (semantic + BM25 + entity) and built-in entity linking; and the real
win of Graphiti is *bitemporality*, 80% of which is `valid_from` / `valid_to` /
`superseded_by` metadata on existing records. *(b)* Any weight-update plan
should wait. OpenAI's RFT is `o4-mini`-only **and the fine-tuning platform is
winding down**; Anthropic has no public fine-tuning API. When weights do become
worth touching, it is for **voice and speed only** — and a style LoRA on
accepted drafts costs roughly **$20** for a 50M-token run on Tinker.

**And one cautionary tale to design against.** The **RLUF** production study
optimized a deployed model against an implicit user signal at scale (1M
conversations, ~100K with reactions; reward model 0.85 AUROC, **0.95 Pearson
with actual online reaction-rate change**). Aggressive optimization won **+28%
on the target metric** — and made the model **end conversations** ("bye" in
**2.8% of responses vs 0.72% baseline**), dropped helpfulness **4–16%**, and
produced a reward model that **penalized valid safety refusals**, because users
don't react positively to being told no. Attune's analogue is exact: an
assistant that maximizes approve-rate by proposing only trivial, safe drafts
and staying silent on the hard ones. **Any edit-burden metric needs a coverage
denominator or it will reward-hack itself into uselessness.**

## 6. Scorecard

| Dimension | Attune | Verdict |
|---|---|---|
| Safety spine — approvals, freshness re-checks, idempotency, actor allowlists, structural refusals, hash-chained audit | Built and tested | **Ahead of the market.** ClawsBench's 7–33% unsafe-action rates are the external case for it |
| Earned autonomy ladder | Built, never graduates in-product; scoped grants have no earning mechanism at all | **Best idea in the product, unrealized.** Externally validated by the Digital Apprentice framework and by Skej pricing unsupervised mode as premium |
| Event-driven ingestion | Pub/Sub push + polling | **Ahead of both consumer leaders**, and unmarketed |
| Self-hosting / no vendor dependency / data portability | Real | **Uniquely defensible** given §1's body count |
| Multi-tenant isolation engineering (hosted) | RLS, memberless SECURITY DEFINER owners, KMS vault, two-phase audit outbox, capability gateway | Exceeds what most products ship — **and is wired to one dormant capability** |
| Action surface | 6 writes; no undo, no batching, no expiry; Chat/Slack write-dead | **Far behind.** Claude has full read/write Calendar; Outlook Agent Mode does the whole triage-and-reschedule loop |
| Learning loop | Deterministic half works; **semantic half writes sender-less strings and cannot function** | **Broken, and it is the reason the product exists** |
| Evals | No metric computed anywhere; live eval cannot execute | **Behind a published LangChain tutorial** |
| Proactivity design | Static brief + arrival-order caps | **Behind the market's own retraction.** Needs ranking and user-authored routines |
| Performance | ~64 serial Google calls per brief (10–25s); 60–90s per 25-thread batch; zero concurrency; no prompt caching | **Behind.** Glean now competes on **cost-per-answer** (Waldo: ~50% lower latency, ~25% fewer tokens) |
| Model layer | Chat Completions text-in/text-out; no tool calling, structured outputs, caching, or streaming | **A capability floor mislabelled as neutrality** |
| Interop | MCP client only; contract predates a breaking spec | **Missing the position it is best suited to hold** |
| Voice / mobile / meeting capture | None | Behind — but capture carries live legal exposure; consume over MCP instead |
| Engineering economics | 2.7% code reuse across two implementations of one product | **A compounding velocity tax** |

**Summary.** Attune has built the hard, unglamorous half of a category the
market keeps failing at, and has not yet built the half that makes anyone want
it. The safety, durability, and auditability work is genuinely ahead — and it
is sitting under a six-action product whose learning loop does not close and
whose flagship surface is the one thing three major vendors measured and
retired. The correction is not feature parity with Gemini, which is
unwinnable and free. It is to make the compounding loop real and provable,
widen the action surface enough to be worth trusting, and market the two things
nobody else can offer: an agent that earns authority on an evidence trail you
can audit, and one that cannot be switched off by its vendor.
