# Aide-de-camp: Design, Architecture, and Roadmap

*A self-learning assistant over Gmail, Calendar, Google Chat, and Slack,
running on Fuel iX. Slack text is the first supported live surface; Google
Chat app authentication and voice remain later work.*

> **Name & license (settled):** Project name **Aide-de-camp**, PyPI package `aidedecamp`, licensed **MIT**. The metaphor — a trusted officer who acts on a principal's behalf within delegated authority — is a deliberate match for the earned-autonomy ladder in §3.2. Renamed from the earlier working title "Steward" to avoid collision with several existing near-identical GitHub projects (study8677/Steward, rcarmo/python-steward, googlicius/obsidian-steward). The Fuel iX bearer-token adapter (§4.5, §7) ships as a **separate, generically-named package** (candidate: `bearer-openai`) so the enterprises hitting the same gateway-auth problem can find it without knowing this project exists.

---

## 0. Design philosophy

Three ideas drive every decision below:

1. **Memory is the product.** Any wrapper around an LLM can summarize an email. What makes this a *personal* assistant is that it gets better at being *your* assistant every week: it remembers who your recurring meeting is really about, that you never take calls before 9am, that "the TELUS thing" means a specific project, and that you edit its drafts to cut the throat-clearing. Everything else is plumbing around that.
2. **Autonomy is earned, not granted.** The agent starts read-only and propose-only everywhere. It graduates to acting on its own only in narrow categories, one at a time, based on a track record you can see. This is both a safety property and a trust-building UX.
3. **One brain, many doors.** Slack, Google Chat, browser, and voice are interaction surfaces, not separate assistants. There is one orchestrator and one memory store behind all of them, so a preference you correct in Slack is already in effect when you ask the same question by voice that evening.

---

## 1. What a personal assistant agent actually needs to do

It helps to split this into five capability layers. Most "AI assistant" products blur these together, which is why they feel shallow after the first week.

### 1.1 Perceive - ingest signal from your world
- New mail arriving (Gmail)
- Calendar changes: new invites, declines, reschedules, upcoming events (Calendar)
- Messages directed at it or mentioning it (Slack, Google Chat)
- On explicit request: Drive documents, past threads, contacts/People API

### 1.2 Understand - turn signal into structure
- Triage: urgent vs. routine vs. noise, using sender, thread history, and your past reactions
- Extraction: who's involved, what's being asked, what deadline is implied, which project this belongs to
- Linking: "this email is about the same thing as that calendar hold and that Slack thread"

### 1.3 Act - do the work, at whatever autonomy level it has earned
- Draft a reply; draft a scheduling proposal; create a calendar hold; create a follow-up reminder
- Eventually (later phases): send, decline, reschedule, delegate

### 1.4 Communicate - talk to you, not just about your data
- On-demand Q&A: "what's on my plate today," "who's the deal lead for the Acme renewal"
- Proactive, digestible surfacing: a morning brief, a nudge that a thread has gone quiet, a heads-up that two meetings just collided
- Inline approvals: cards with buttons, not walls of text, when a decision is needed

### 1.5 Learn - get better at 1–4 over time
- Notice when you edit a draft before sending, and why
- Notice what you ignore vs. what you act on
- Consolidate raw events into durable facts and routines
- Forget or supersede facts that are no longer true, without being told explicitly every time

Most of what follows is architecture for layer 5, because that's the part with no shrink-wrapped answer.

---

## 2. How it learns over time - memory architecture

### 2.1 The four kinds of memory it needs

This is the same split cognitive science uses for human memory, and it maps cleanly onto what production agent-memory systems (Letta/MemGPT, Zep/Graphiti, Mem0) have converged on independently:

| Memory type | What it holds | Example | Lifespan |
|---|---|---|---|
| **Working memory** | The current task's context | "User is replying to this specific email thread right now" | Single episode |
| **Episodic memory** | A log of what happened | "On July 3, drafted a decline for the 2pm sync, user sent as-is" | Indefinite, but low-priority to keep in context |
| **Semantic memory** | Durable facts about your world | "Priya is the PM for Project Falcon"; "user's manager is Dana" | Indefinite, until superseded |
| **Procedural memory** | Learned routines / how-to | "When a recruiter cold-emails, draft the standard polite decline"; "Friday 4pm slots are never proposed" | Indefinite, until you change it |

The mistake most DIY agent projects make is treating all of this as one undifferentiated vector store. That's why they "remember" in the sense of retrieving old text, but never actually *learn* - a fact from January and its contradiction from June both come back as equally plausible search hits. Solving that requires memory that understands time.

### 2.2 The mechanism: capture, consolidate, retrieve

**Capture (continuous, cheap).** Every interaction produces raw episodic events: an email arrived, a draft was proposed, the draft was edited before sending, a meeting was auto-scheduled and later moved. Two capture signals matter most for personalization and are consistently underused:

- **Correction capture.** When you edit an agent-drafted email before sending, diff the draft against what was actually sent. That diff *is* a preference signal ("too formal," "cut the second paragraph," "always CC Dana on client mail") - far richer than a thumbs-up/down.
- **Implicit action signal.** Approved without edits, edited, ignored, explicitly rejected. Weight learning by this, not just by things you say to it directly.

**Consolidate (periodic, not real-time).** Raw episodes are noisy and expensive to keep in context forever. On a schedule (e.g., nightly), a background pass reviews recent episodes and promotes durable, repeated patterns into semantic or procedural memory, the way Letta's "sleeptime" agents or a nightly Graphiti ingestion pass work. This is also where facts get **superseded rather than deleted**: if "Priya is the PM for Falcon" changes to "Marcus is the PM for Falcon," a good memory layer keeps both facts with validity windows, rather than silently overwriting history (this is Graphiti's bi-temporal model: every fact has a time it became true and, if applicable, a time it stopped being true).

**Retrieve (per-turn, cheap and targeted).** At inference time, the agent pulls only what's relevant to the current task - not "everything about Priya," but "everything about Priya *relevant to scheduling*." This is standard RAG-over-memory, but the quality of what you're retrieving *from* depends entirely on capture and consolidation being done well upstream.

### 2.3 Picking the memory substrate

You don't need to build this from scratch - there's real prior art, and the tradeoffs are now well understood:

| Option | Model | Best for | Tradeoff |
|---|---|---|---|
| **Mem0** (self-hosted, OSS) | Vector store + optional lightweight graph; agent-agnostic `add()`/`search()` API | Fastest path to "the agent remembers things across sessions" | No native temporal model - a superseded fact and its replacement are both just retrievable memories, with no built-in sense of which is current |
| **Zep / Graphiti** (self-hosted OSS core, or managed Zep Cloud) | Temporal knowledge graph (Neo4j-backed); every fact has a validity window | Reasoning about *people, relationships, and projects that change over time* - exactly your use case (roles change, project ownership changes, priorities change) | More moving parts to run yourself (a graph DB); steeper setup than Mem0 |
| **Letta / MemGPT** | Full agent runtime with tiered, self-editing memory (agent decides what to write to memory via tool calls) | Cases where you want the *agent itself* to reason about what's worth remembering | You're adopting a runtime, not just a memory library - bigger architectural commitment if you're pairing it with your own orchestrator |
| **Roll your own** (Postgres + pgvector + a `facts` table with `valid_from`/`valid_to`) | Whatever you design | Full control, fits your existing model-agnostic-orchestrator instincts from Stagecraft | You reinvent invalidation logic, entity resolution, and retrieval ranking - all solved problems elsewhere |

**Recommendation:** start with **Mem0, self-hosted**, for the first working version - it gets you cross-session memory in days, not weeks, and is genuinely framework-agnostic (works fine sitting next to LangGraph). Plan a migration path to **Graphiti** once you hit the point where "who owns what, as of when" questions start mattering - which, given you're tracking people, projects, and org relationships across TELUS verticals, will happen faster than in a typical consumer use case. Don't build a custom memory layer for v1; there's no differentiated value in reinventing entity resolution and temporal invalidation when Graphiti has already done it and is Apache-licensed.

### 2.4 How you'll know it's actually learning

Borrow the eval discipline this space has converged on rather than eyeballing it. Keep a small, growing set of your own "memory test" scenarios, structured like the categories in the LoCoMo and LongMemEval benchmarks used to evaluate agent memory: single-session recall, multi-session recall, preference recall, and - the one that matters most for you - **knowledge update** ("if I told the agent Priya owns Falcon in March and Marcus owns it in June, and I ask in July, does it say Marcus, and does it know *when* that changed if asked?"). Run these periodically as a regression check whenever you change the memory pipeline.

---

## 3. Interaction design

### 3.1 Channels, and what each is actually good for

| Channel | Best for | Notes |
|---|---|---|
| **Slack** | Fast, asynchronous, text-first interaction; approvals via buttons | Build on the **Bolt** framework's `Assistant` class - it's purpose-built for exactly this (side-panel assistant UI, suggested prompts, streaming responses, feedback buttons) rather than a bare bot |
| **Google Chat** | Same role as Slack, for anyone/anything that lives in the Workspace side of your world | Event-driven app model: subscribe to space/message events via the Workspace Events API, or respond synchronously to @mentions and slash commands |
| **Browser** | Deep work: reviewing a full daily brief, editing a memory fact directly, auditing what the agent has learned about you | A lightweight web app is the natural home for "let me see and correct everything you know about me" - this is also your escape hatch when a chat window is too narrow a UI for the task |
| **Voice** | Hands-free moments: commute, walking, "what's my next meeting" | Treat as the *last* channel you build, and expect a cascaded pipeline rather than true speech-to-speech (see §5.5) |

### 3.2 The autonomy ladder

This is the single most important interaction-design decision, and it should be explicit and per-category, not a single global "autonomy level":

| Rung | Behavior | Example |
|---|---|---|
| 1. **Read-only** | Agent observes and summarizes; takes no action | "You have 3 unread emails from clients, here's what they want" |
| 2. **Propose, wait for approval** | Agent drafts the action; nothing happens until you confirm | Drafts a reply; shows it in a Slack card with Send / Edit / Discard |
| 3. **Act, notify after** ("human-on-the-loop") | Agent executes low-risk, reversible, narrowly-scoped actions on its own, and tells you what it did | Auto-declines an obvious spam meeting invite; auto-labels newsletters |
| 4. **Fully autonomous** | No notification needed | Reserved for routines you've explicitly graduated after a track record at rung 3 |

Two rules make this safe and legible:
- **Scope by action type and by domain, never globally.** "Can draft replies" and "can send replies" are different permissions. "Can act on calendar holds with no external attendees" and "can act on anything calendar-related" are different permissions. Build the permission model as a matrix (action type × data domain), not a slider.
- **Prefer task-scoped or time-scoped grants over standing ones**, and log every action taken with its reasoning, both for your own trust-building and because you'll want an audit trail once this touches anything TELUS-adjacent.

In practice, expect almost everything to live at rung 1–2 for months. That's fine - it's also where most of the value is (you already saved the 5 minutes of reading and drafting; you just kept the final judgment call).

### 3.3 Interaction patterns, concretely

- **Morning brief** (proactive, rung 1): a short digest pushed to your default channel - meetings today with prep notes pulled from the last thread on each, anything overnight that needs a same-day response, anything that's gone quiet longer than it should have.
- **On-demand Q&A** (any time, rung 1): "what did Priya say about the Falcon timeline" retrieves and answers from memory + live search over recent mail, doesn't just re-fetch everything.
- **Inline approval** (rung 2): a drafted reply arrives as a card, not a wall of text, with buttons. Editing it before sending is itself a learning signal (§2.2).
- **Quiet-thread nudge** (proactive, rung 1→3 as trust builds): "you haven't heard back from Marcus in 4 days on the contract redline, want a follow-up drafted?"

---

## 4. Technical architecture

### 4.1 High-level shape

```
                         ┌─────────────────────────────┐
                         │      Memory & Facts Store    │
                         │  (Mem0 → later Graphiti)     │
                         │  episodic / semantic /       │
                         │  procedural, all self-hosted │
                         └───────────▲──────────────────┘
                                     │ read/write
┌───────────────┐   events    ┌──────┴───────────┐   model calls   ┌──────────────┐
│  Event sources │───────────▶│   Orchestrator    │────────────────▶│   Fuel iX     │
│  Gmail push    │             │   (LangGraph)     │◀────────────────│  (Sonnet/    │
│  Calendar push │             │  - triage graph   │   completions   │   Haiku/GPT/ │
│  Chat events   │             │  - draft graph    │                 │   Gemini)    │
│  Slack events  │             │  - schedule graph │                 └──────────────┘
└───────────────┘             │  - brief graph    │
                                │  checkpointed,    │
                                │  pausable for HITL│
                                └──────┬────────────┘
                                       │ actions / messages
                       ┌───────────────┼────────────────┐
                       ▼               ▼                ▼
                 ┌──────────┐   ┌─────────────┐   ┌────────────┐
                 │  Slack   │   │ Google Chat │   │  Browser / │
                 │  (Bolt)  │   │  app        │   │  Voice     │
                 └──────────┘   └─────────────┘   └────────────┘
                       │               │
                       └───────┬───────┘
                               ▼
                 ┌───────────────────────────┐
                 │ Google Workspace via MCP   │
                 │ (Gmail, Calendar, Chat,    │
                 │  People, Drive)            │
                 └───────────────────────────┘
```

### 4.2 Orchestrator: why LangGraph

You want an orchestration layer that is (a) genuinely model-agnostic, since Fuel iX's whole value is letting you route across Sonnet, Haiku, GPT-5.4/5.5, and Gemini 3.5 Flash; (b) built for long-running, resumable, human-in-the-loop workflows, since "draft and wait for approval, possibly hours later" is your core interaction pattern, not an edge case; and (c) able to persist state across restarts, since this is a background service, not a CLI you run once and forget.

**LangGraph** is the strongest fit on all three: it models each workflow as a graph with typed state and conditional edges, has first-class checkpointing (in-memory, SQLite, or Postgres) so a paused "waiting for your approval" state survives a restart, and has purpose-built human-in-the-loop interrupt/resume primitives. CrewAI is faster to a demo but weaker on exactly the durability and branching control this project needs long-term. The OpenAI Agents SDK and Google ADK are excellent but pull you toward a single model family, which defeats the point of routing through Fuel iX.

Model this as **several small graphs, not one giant one**: a triage graph (per incoming email/message), a draft-and-approve graph, a scheduling graph, and a daily-brief graph. Small, single-purpose graphs are easier to reason about, checkpoint, and hand off between channels.

> **Built (2026-07):** the draft-and-approve graph is the only LangGraph
> graph. Triage and the daily brief are plain functions. Calendar conflict
> detection feeds the same approval graph to create a tentative hold at
> PROPOSE; invite responses and rescheduling remain unbuilt.

*Aside: this is a different shape than Stagecraft.* Stagecraft orchestrates coding tools as a team for a bounded, interactive session. This is a long-running, event-driven, always-on service reacting to the outside world at unpredictable times. Some of the model-agnostic-adapter thinking will transfer directly; the execution model (durable, resumable, triggered by webhooks) won't.

### 4.3 Connecting to Google Workspace

Google now ships **official, managed MCP servers** for Gmail, Calendar, Chat, People, and Drive (Developer Preview as of mid-2026) - remote MCP endpoints (e.g. `gmailmcp.googleapis.com`, `calendarmcp.googleapis.com`) that any MCP-speaking client can connect to with OAuth, inheriting your normal Workspace permissions. This is the right foundation to build on rather than hand-rolling OAuth + REST calls for each API, and it means your orchestrator's tool layer is just "an MCP client," regardless of which Google product it's calling. Community MCP servers (`google-workspace-mcp`, `mcp-google-workspace`) exist too and are worth a look if the managed ones prove too limited during preview, but prefer Google's own given the security/governance inheritance story.

Since TELUS sign-off on MCP server access for the corporate account is
genuinely uncertain (per §4.7), the implementation defines a small internal
interface with MCP and direct-OAuth adapters. **Current deployment reality:**
`direct_oauth` is the only runtime-wired mode; the MCP adapter still requires
an injected transport. See `docs/deployment.md` rather than treating this
design preference as an operator instruction.

For **event ingestion** (the thing that makes this feel alive rather than poll-based):
- **Gmail**: push notifications route through **Cloud Pub/Sub**, not a direct webhook - you call `users.watch` with a Pub/Sub topic, Gmail publishes a `{emailAddress, historyId}` pointer on change, and you call `users.history.list` to fetch what actually changed. The watch **expires every 7 days and must be renewed** (renew daily as routine maintenance, don't wait for expiry).
- **Calendar**: push notifications are a direct HTTPS webhook (no Pub/Sub needed) - you register a notification channel per calendar resource and get POSTs on change.
- **Google Chat**: subscribe via the **Workspace Events API** for real-time space/message events, or handle synchronous interaction events (@mentions, slash commands, card clicks) directly for the conversational side.

### 4.4 Connecting to Slack

Build on **Bolt** (Python or JS) using its `Assistant` class, which exists specifically for this pattern: a side-panel assistant UI, suggested prompts on thread start, streaming responses, and feedback buttons, all handled by Bolt's built-in event plumbing (`assistant_thread_started`, `message.im`, thread-context tracking). This is meaningfully less work than building a generic bot on raw Events API and reimplementing conversational state yourself.

### 4.5 Fuel iX integration specifics

Since Fuel iX exposes an OpenAI-compatible `/chat/completions` surface: any OpenAI-compatible client library (including `langchain-openai`, which LangGraph uses natively) should point at it by setting `base_url` to `https://api.fuelix.ai` and supplying the bearer token as the client's API key - OpenAI's own auth scheme is `Authorization: Bearer <token>` under the hood, so this is a config difference, not a protocol difference.

**Confirmed: the token is long-lived and rotated manually**, not a short-lived OAuth token needing an active refresh flow. That simplifies things: no refresh-token dance to build. What's still worth building, and is exactly the shape of the standalone open-source package described in §7:
- Treat the token as swappable config (env var or secrets store entry), never hardcoded, so rotating it is a redeploy/restart, not a code change
- Fail loudly and specifically on a 401 (log "Fuel iX token rejected, needs manual rotation" rather than a generic retry loop that silently burns time or masks the real problem)
- Build the adapter as a small, generic wrapper: an OpenAI-compatible client that takes a bearer token instead of an API key, with no Fuel iX-specific logic baked in. Fuel iX is very unlikely to be the only enterprise gateway that made this exact choice, so this is worth publishing as its own tiny package rather than burying it inside the assistant's codebase

**Model routing.** With Sonnet, Haiku, GPT-5.4/5.5, and Gemini 3.5 Flash all available behind one endpoint, route by task shape rather than defaulting to one model everywhere:
- Cheap/fast classification (is this urgent? is this spam?) → Haiku or Gemini 3.5 Flash
- Drafting, reasoning about scheduling conflicts, multi-step planning → Sonnet or GPT-5.4/5.5
- Memory consolidation passes (nightly, not latency-sensitive, but needs to reason carefully about contradictions) → your more capable model, since correctness compounds over time here

This routing logic belongs in the orchestrator, not hardcoded per graph, so you can tune it centrally as pricing/quality shifts.

### 4.6 Deployment shape

This needs to be an **always-on background service**, not an on-demand tool: Gmail/Calendar watches, Chat event subscriptions, and Slack's socket/event connection all assume something is listening continuously. A small persistent process (container on a VM, or a lightweight always-on box) is a better fit than serverless functions for the orchestrator core, though the individual webhook receivers (Gmail Pub/Sub push endpoint, Calendar webhook endpoint) can be thin serverless functions in front of it if you want to minimize what's exposed to the internet directly.

**Two fully separate deployments, not one shared instance.** *(Updated 2026-07: both now run on GCP — personal moved off the original home-server plan onto its own GCP project, once one was available; see `docs/deployment.md`. The reasons below for keeping them separate are unchanged — only the infrastructure personal runs on changed, not the separation itself.)* This isn't just a consequence of the infrastructure being different in each case, it's the right call independent of that, for three reasons: (1) governance legibility, a TELUS admin reviewing this needs to see a clean, scoped footprint, one deployment touching one mailbox with its own credentials, not a shared runtime that happens to be internally namespaced; (2) blast radius, a bug, a memory-consolidation mistake, or a prompt-injection attempt on one side can't touch the other if they don't share a process, a memory store, or a filesystem; (3) it matches the reality that trust and permissions genuinely differ between the two contexts, so the code should express that as two configurations of the same open-source project, each with its own credentials, its own Mem0 instance, and its own audit log, rather than trying to encode the boundary as an internal if-statement. The only thing that should be shared between them is the codebase itself (and the Fuel iX bearer-token adapter as a dependency both install).

**Gizmos: ruled out, confirmed by their own team.** Directly from the Gizmos developers: it's built for short-lived requests, under 30 seconds, and isn't intended for agentic workloads. That matches the concern raised in §4.6 exactly, no need for a spike test, this settles it. **Both sides run on a GCP Compute Engine VM**, each in its own project.

Being on GCP specifically, rather than an arbitrary VM host, is a genuine advantage here, not just a fallback:
- **Gmail's push mechanism is Cloud Pub/Sub already**, so the watch topic lives in the same GCP project as the VM, no cross-cloud plumbing, and no need for an inbound webhook at all: use a **pull subscription** and have the VM poll/stream from Pub/Sub directly (outbound call, not an exposed port).
- **Slack's Socket Mode** is also outbound-only, no public endpoint needed there either.
- **Google Chat's Workspace Events API** supports Pub/Sub delivery too, so it can follow the same pull pattern as Gmail.
- **Calendar push notifications are the one genuine exception**: unlike Gmail and Chat, Calendar's watch API only delivers via a registered HTTPS webhook, there's no Pub/Sub option. Keep this from touching the main orchestrator box directly: a tiny, stateless Cloud Run service or Cloud Function that does nothing but validate the notification and republish it onto a Pub/Sub topic the VM pulls from. That means the VM holding your credentials, memory, and reasoning never has an open inbound port at all, which matters a lot given the OpenClaw lesson in §8.1 about what happens when a privileged agent process is directly reachable by untrusted input.
- **Secret Manager** for the Fuel iX bearer token, Google OAuth client secret, and Slack tokens, rather than a plain `.env` file on disk, cheap at this scale and makes the "rotate the Fuel iX token" workflow (§4.5) a Secret Manager update plus a service restart, not a redeploy.
- A small VM is genuinely enough to start: e2-small or e2-medium comfortably runs the orchestrator, Mem0, and its vector store side by side as containers; resize later if the memory-consolidation workload grows.
- Give the VM's service account least-privilege IAM (Pub/Sub subscriber, Secret Manager accessor, nothing broader), consistent with the permission-matrix philosophy in §3.2 applied to infrastructure, not just agent actions.

Net effect: the personal deployment and the TELUS deployment (each its own GCP project + Compute Engine VM) end up structurally identical, same container, same codebase, different config and credentials, which is exactly the symmetry the two-deployment model in the paragraph above is aiming for.

### 4.7 Governance and scope, given the TELUS context

Two things worth deciding deliberately before you connect anything:
- **Personal vs. corporate scope: both, kept fully separate** (see §4.6). TELUS's Workspace admin settings may also require explicit approval for third-party OAuth apps (including MCP servers) requesting Gmail/Calendar/Chat scopes on a corporate identity; since that's genuinely uncertain, build the connector layer as a swappable interface (Google's managed MCP servers, or direct OAuth + REST via `google-api-python-client`, chosen per deployment via config) rather than committing to one path in code. That way a "no" from TELUS IT on MCP access is a config change, not a redesign.
- **Audit trail as a first-class feature, not an afterthought.** Given your own team's focus on XAI, treat "why did the agent do that" as a designed capability from day one: every proposed or taken action should carry a short, structured reason, retrievable later. This also happens to be exactly the kind of explainability discipline that's cheap to build in early and expensive to retrofit.

---

## 5. Voice - a deliberately later phase

Voice deserves its own section because the obvious approach (a single "speech-to-speech" model like OpenAI's Realtime API or Gemini Live) **won't sit behind Fuel iX** the way your text traffic does. Those are persistent-WebSocket, audio-native models - a different product surface than the `/chat/completions` proxy Fuel iX exposes. Two honest paths:

1. **Cascaded pipeline (works with Fuel iX as-is):** device or provider speech-to-text → text turn goes through your existing Fuel iX-backed orchestrator exactly like a chat message → text-to-speech on the way back. Higher latency (low seconds, not milliseconds), but it reuses 100% of the brain you already built, including memory.
2. **Native speech-to-speech (if ever wanted):** a separate integration directly with something like Gemini Live or OpenAI's Realtime API, bypassing Fuel iX for the voice turn specifically, then handing off to your Fuel iX-backed agent for anything requiring deep reasoning or memory lookup. This is architecturally a second, parallel front door, and meaningfully more engineering than the cascaded option.

Recommendation: build voice last, start with the cascaded approach, and only invest in native speech-to-speech if the latency of the cascaded version genuinely bothers you in daily use.

---

## 6. Roadmap

Each phase should produce something you actually use daily before moving to the next - this is the difference between a project that compounds and one that stalls at 80% forever.

> **Implementation status (2026-07):** everything below marked ✅ is built and
> covered by the current offline suite (571 passing); the first credential
> checks and terminal brief have been exercised against a personal account,
> but the service has not been deployed or left running. No phase's "Done
> when" usage bar is met yet; that requires the work in `CLAUDE.md`'s "Next
> steps." Details and rationale for each ✅ item live in
> `docs/decisions.md`.

### Phase 0 - Foundations (prove the loop end to end)
- ✅ Stand up the LangGraph orchestrator with a Fuel iX-backed model client (incl. token refresh handling)
- ✅ Stand up Mem0 self-hosted as the memory store
- ✅ Pick **one** channel (Slack - fastest to prototype with Bolt's Assistant template) and **one** data source (Gmail, read-only) — *note: implemented via a plain `@app.event("message")` handler rather than Bolt's `Assistant` class, which also offers suggested-prompts/streaming UI not yet used*
- ✅ Ship a v0 morning brief: pulls unread/important mail, summarizes it, and
  posts on the configured daily scheduler.
- ❌ **Done when:** you get a genuinely useful daily brief in Slack, generated from your real inbox, for a full week without babysitting it — blocked on the entrypoint, not the logic

### Phase 1 - Read-only assistant, both data sources, two channels
- ✅ Add Calendar (read-only) — `list_events`/`create_hold` on the connector, plus push-notification ingestion (`ingestion/calendar_watch.py`/`calendar_sync.py`, design 4.6's one genuine webhook exception, handled via the same thin-republisher pattern as Gmail/Chat) — reconciliation stops at changed-event-ids; nothing reacts to them yet (see Phase 3's scheduling gap)
- ⚠️ Add Google Chat as a second channel — Cards v2 rendering, Workspace
  Events ingestion, and the interaction republisher are implemented offline;
  a distinct Chat app-auth credential is not runtime-wired, so live cards are
  not yet deployment-ready.
- ✅ Add conversational Q&A backed by memory + live retrieval — both channels now share it: `dispatcher.handle_chat_message`/`handle_slack_message` route through the same `_respond_to_message` → `_converse`
- ✅ Start correction/implicit-feedback capture (`memory/signals.py`, wired into the draft-approve graph's `capture` node) — already doing more than "start," see Phase 2
- **Done when:** you trust the brief and Q&A enough to check them before checking your inbox directly — blocked on actual deployment, not on remaining code

### Phase 2 - Draft assistance (rung 2 autonomy)
- ✅ Draft replies delivered as interactive Slack cards with Approve / Edit /
  Reject. Chat uses the same graph but remains blocked on app authentication.
  Calendar conflicts can propose tentative holds through that graph.
- ✅ Wire up correction-diff capture properly: every edit-before-send becomes a structured preference signal (`capture_correction`)
- **Done when:** more than half of routine replies start from an agent draft you only lightly edit — a usage claim, unverified with no deployment yet

### Phase 3 - Narrow autonomy (rung 3, then rung 4 for specific routines)
- Graduate specific, low-risk, reversible actions to "act, notify after" (e.g., auto-decline obvious spam invites, auto-file newsletters)
- Build the permission matrix UI (even a simple one) so you can see and adjust what's autonomous, per action type × domain
- ✅ Full audit log with reasoning, queryable — pulled forward from Phase 3 and already built (`audit/log.py`'s `JsonlAuditLog`), ahead of the rest of this phase
- **Done when:** you've graduated at least 2–3 routines to autonomous and haven't had to claw one back

### Phase 4 - Memory maturity
- Migrate from Mem0 to Graphiti/Zep for temporal, relationship-aware memory (who owns what, as of when)
- Nightly consolidation pass: promote repeated patterns to procedural memory, supersede outdated facts rather than overwrite
- Build your own memory-quality regression set, modeled on LoCoMo/LongMemEval categories, and run it after every memory-pipeline change
- **Done when:** you can ask "who owns Project X now vs. in March" and get a correct, time-aware answer

### Phase 5 - Browser surface
- A lightweight web app: full daily brief, direct memory browsing/editing ("here's everything you know about Priya, correct anything wrong"), permission matrix management
- **Done when:** this becomes your default place to *audit and correct* the agent, rather than just consume its output

### Phase 6 - Voice
- Cascaded STT → Fuel iX orchestrator → TTS, starting with a narrow use case (e.g., "what's my next meeting," "read me today's brief")
- Only pursue native speech-to-speech afterward, and only if latency genuinely bothers you in daily use
- **Done when:** you'd reach for voice over typing in at least one real daily scenario (e.g., walking to a meeting)

### Phase 7 - Multi-channel unification
- Presence-aware routing: don't push the same nudge to both Slack and Chat if you've already seen it in one
- Confirm memory and context are genuinely shared: a correction made by voice shows up in a Slack draft the same day
- **Done when:** it stops feeling like "three assistants that happen to share a database" and starts feeling like one assistant with three doors

---

## 7. Decisions settled, and what's still open

### Settled
1. **Scope: both accounts, two fully separate deployments.** Personal Google account + TELUS Workspace account, each with its own credentials, its own Mem0 instance, its own audit log. MCP vs. direct OAuth is a per-deployment config choice, not a hard commitment, in case TELUS IT doesn't approve MCP server access on the corporate identity.
2. **Where this runs.** Both personal and TELUS run on GCP Compute Engine VMs, each in its own GCP project (updated 2026-07 — personal was originally planned for a home server; see `docs/deployment.md` for the concrete setup). Gizmos was ruled out directly by its own developers: it's built for short-lived requests (under 30 seconds) and isn't intended for agentic workloads, which confirms the concern raised earlier without needing a spike test.
3. **Memory store: Mem0, self-hosted.** Two separate instances, one per deployment, not one shared instance with internal namespacing.
4. **Open source: all of it, as its own standalone project** (not folded into Stagecraft). The Fuel iX integration specifically becomes a small, generic, publishable package: an OpenAI-compatible client that authenticates with a bearer token instead of an API key, with no Fuel iX-specific logic baked in, since Fuel iX is unlikely to be the only enterprise gateway that made this choice.

5. **Project name and license (settled).** Name **Aide-de-camp** (`aidedecamp` on PyPI), **MIT** licensed, matching the permissive norm of the surrounding ecosystem (Mem0, LangGraph tooling, community Workspace MCP servers) and keeping TELUS dependency review frictionless. The Fuel iX adapter is a separate generic package (`bearer-openai` candidate), not named after the assistant, to maximize its reuse by others behind the same kind of enterprise gateway.

### Still open
- **Agent-tool quota/tiering** - Google's new standardized usage tiers for agentic Workspace access (rollout from May 1, 2026; 60 days' notice for existing projects) specifically target always-on, high-volume watch/poll patterns. Confirm the intended Gmail/Calendar watch-renewal and polling cadence against current quota docs before Phase 0 ingestion is built.

### Answered (2026-07)
- **Google Chat action-layer API design** - both, not either/or: card-interaction events (approve/reject) handle button clicks, and the Workspace Events API pull pattern (`ingestion/chat_events.py::ensure_subscription`, mirroring the Gmail watch lifecycle) handles proactive message ingestion. **Correction (2026-07):** an earlier version of this entry said card-interaction events use "the same thin-republisher pattern as Gmail" - that undersold a real difference. Gmail's republisher only ever forwards to Pub/Sub; Chat's card clicks need a *synchronous* HTTP response, which the credential-holding process can't provide (rule 5), so the actual resume happens asynchronously after the republisher's immediate placeholder ack (`dispatcher.handle_chat_interaction`, pulled via its own Pub/Sub subscription) - see `docs/decisions.md`, "Async Chat card-interaction flow," for the full reasoning and the options that were rejected along the way.

### Verified (2026-07)
- **Fuel iX base URL and model identifiers.** `base_url = https://api.fuelix.ai`.
  Known model identifiers remain centralized in `fuelix.py`. Defaults route
  classification to Haiku 4.5 and other tasks to Sonnet 5; every task has an
  `ADC_MODEL_*` override because entitlements vary by token, and Doctor probes
  the configured routes rather than assuming the catalog is authorization.

---

## 8. Appendix: what already exists, and what it teaches us

Worth surveying before you build, because every one of the categories below has already run into the exact tradeoffs this design has to make, and their public track record is a faster teacher than a whiteboard.

### 8.1 The landscape, by category

| Category | Examples | How they work | Strengths | Weaknesses |
|---|---|---|---|---|
| **Cloud "AI executive assistant"** (all-in-one, subscription) | Lindy, alfred_, Fyxer | Trigger/action workflow engine ("Lindies" in Lindy's case) plus an LLM reasoning layer on top, cloud-hosted, connects to Gmail/Outlook/Slack via OAuth. Drafts wait in a queue for your approval; some categories (spam, newsletters) auto-act. | Genuinely fast to set up (minutes, not weeks); text/iMessage-first UX feels ambient; real memory of writing style over time | No self-hosting, so your memory and configuration aren't portable if you leave; credit-based pricing with usage caps that surprise people; **no exposed audit trail or reasoning log**, so when it mislabels or misfires there's no way to see why; scheduling logic is solid for easy cases (propose 3 slots for a 1:1) but thin for hard ones (negotiating with an external thread, group scheduling) |
| **Self-hosted, general-purpose autonomous agents** | OpenClaw (formerly Clawdbot/Moltbot) and its safety-focused forks (NanoClaw, ZeroClaw) | Kernel-plugin architecture: a channel system (messaging platforms) feeds a gateway, which routes to an agent runtime with persistent memory and a plugin/skills ecosystem that reaches shell commands, browsers, and APIs | Full control, no vendor lock-in, genuinely powerful because it isn't sandboxed away from your system | This is the cautionary tale, worth taking seriously: it went from launch to one of GitHub's most-starred repos in weeks, and shortly after, security researchers documented prompt-injection attacks delivered via ordinary email and chat messages that led to remote code execution and, in reported cases, agents autonomously making purchases or spamming contacts. The root cause is architectural, not a bug: high-privilege system access plus exposure to untrusted input (any email you receive) plus real-world action capability, all in one trust boundary. The safety-focused forks exist specifically to sandbox each agent in its own container after the fact |
| **AI-native email clients ("assisted," not autonomous)** | Superhuman, Shortwave | AI layered into a replacement email client: thread summaries, draft suggestions, bundling/triage views, natural-language search over your mailbox | Genuinely pleasant, fast interfaces; real productivity gain if your bottleneck is typing speed and inbox navigation | By design, they don't remove the judgment layer: you still open, read, and decide on every message. Shortwave's own reviewers put it plainly: "it does not triage your inbox proactively... no one has read them and decided which ones matter." This is the category ceiling of an *assistant* rather than an *agent* |
| **Calendar-specific agents** | Motion, Reclaim.ai | Continuously re-optimizes your calendar around actual capacity rather than just logging due dates ("due date does not equal do date") | Narrow scope done well; good if scheduling chaos is your specific pain point | Solves one slice (your own time-blocking) rather than the cross-channel triage-and-draft problem you're after |
| **Passive personal-memory capture** | Rewind (now Limitless, acquired by Meta and discontinued as of Dec 2025) | Continuously records screen and/or ambient audio, indexes everything locally, answers natural-language questions against the raw capture | Genuinely impressive recall ("what did John say about the budget in Tuesday's meeting") without any explicit note-taking | Two failure modes worth learning from: (1) "remember everything, unstructured" doesn't equal understanding: it's a search index over raw capture, not a system that reasons about what's *true now* vs. *was true then*; (2) company/product mortality is real in this category. An acquisition ended the product for existing users with real notice but real disruption. If this is going to be your most personal, highest-trust data store, that argues for owning it yourself rather than parking it inside someone else's roadmap |
| **Ecosystem-native assistants (big tech)** | Gemini for Google Workspace, Microsoft 365 Copilot, Alexa+/Siri | Built by the platform vendor directly into their own apps, grounded in your data through their own Graph/index, with per-app invocation (a sidebar or "Help me write" button) rather than a standing background presence | Deep, low-friction integration exactly within their own ecosystem; strong enterprise governance story (Copilot inherits Microsoft 365 permissions automatically, for instance) | Locked to one ecosystem, no meaningful reach outside it (a Microsoft shop and a Google shop each need their own); largely **invoked, not proactive** - you still open the sidebar and ask, rather than it noticing something and telling you; and notably, even within one vendor, memory doesn't necessarily travel between their own products (Google has said its Workspace and Enterprise Gemini products don't share memory or context with each other) |

### 8.2 What this validates in the design above

A few of the choices already made in this doc aren't arbitrary. They're direct responses to failure modes the market has already hit:

- **The autonomy ladder is the whole game, and most products sit at one extreme or the other.** Superhuman/Shortwave never really leave rung 1 (they make *you* faster, they don't decide). OpenClaw jumped straight to rung 3-4 with no ladder at all, and that's precisely what turned "reads your email and takes action" into a remote-code-execution vector. Building the graduated ladder in from day one, rather than picking a fixed autonomy level for the whole system, is the thing almost nothing above does well.
- **An exposed audit/reasoning log is a real gap in the market, not just a nice-to-have.** Lindy, probably the most polished product in this list, is explicitly criticized for having none: when it mislabels or misfires, there's no way to see why after the fact. Given your own team's XAI focus, building this in from Phase 0 is a genuine differentiator, not just good practice.
- **The OpenClaw incident is the concrete argument for sandboxing and scoped permissions**, not an abstract security best-practice. "Lethal trifecta" (privileged access + untrusted input + real-world action, all in one trust boundary) describes exactly what an email/calendar/chat agent is, unless you deliberately design against it, which is why §3.2's per-action, per-domain permission matrix and §4.7's audit trail aren't optional hardening, they're the difference between this project and OpenClaw's failure mode.
- **Owning your memory store is a hedge against vendor mortality, not just a technical preference.** Rewind/Limitless shows that even a well-loved, well-reviewed product in this exact category (personal memory) can disappear from under you via acquisition. Self-hosting Mem0/Graphiti isn't just about cost or customization, it's about making sure the thing that took a year to teach your agent about your life doesn't evaporate because a company got acquired.
- **Nobody has actually solved cross-channel, proactive, judgment-level assistance yet.** The all-in-one cloud players get closest but are single-vendor and closed. The ecosystem-native assistants are deep but reactive and walled off from each other. That gap, an assistant that's genuinely proactive, genuinely learns, and isn't locked into one vendor's four walls, is the actual opportunity here, and it's why this design leans on a model-agnostic orchestrator (LangGraph) and open memory tooling (Mem0/Graphiti) rather than adopting any single product's architecture wholesale.
