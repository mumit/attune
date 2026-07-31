# 34 — MCP: spec migration, and Attune as a server

**Phase P8** · `docs/plan-2026-h2.md` · **Depends on:** 30, 31 · **Blocks:** nothing

---

Read `CLAUDE.md` (the MCP boundary rule and the "no public port" rule),
`docs/mcp-contract.md`, `connectors/mcp_client.py`,
`hosted/capability_gateway.py`, and the P8 section of
`docs/plan-2026-h2.md`.

## Problem

Two separate problems that share a protocol.

**The contract is about to break.** The 2026-07-28 MCP specification is
described by its maintainers as the largest change since authorization was
added. The core **goes stateless**: the `initialize`/`initialized` handshake and
the `Mcp-Session-Id` header are eliminated, and client information travels in
per-request `_meta`, so servers that needed sticky sessions can sit behind plain
round-robin. **Roots, Sampling, and Logging are deprecated** (functional through
roughly May 2027 under a new 12-month lifecycle policy). A new extensions
framework adds **MCP Apps** and **Tasks** — async long-running operations with
polling, mid-flight input, and durable handles. `docs/mcp-contract.md` is at
v1.1 and predates all of it; the Streamable HTTP session assumptions in
`connectors/mcp_client.py` are exactly what changed.

**Attune consumes and exposes nothing.** Slack, Calendly, Cal.com, Granola,
Glean, and Google Workspace all now expose MCP servers so other agents can call
them. Slackbot became an MCP client into 6,000+ apps; Dropbox shipped Reclaim as
an app inside ChatGPT. Attune's importance profile, triage, brief, and pending
approvals are exactly the kind of capability an assistant-of-assistants would
want — and there is no way to reach them. For a one-principal product,
**being the memory-and-importance layer other agents query is a stronger
position than competing as another front door** against products that are free.

## Task

1. **Migrate the client to the 2026-07-28 spec.** Remove the handshake and
   session-header assumptions, move client info to per-request `_meta`, stop
   using deprecated Roots/Sampling/Logging, and bump `docs/mcp-contract.md` to
   v2.0 with a compatibility note stating which server versions each client
   version supports. `CLAUDE.md` requires that changes to required tools or
   envelopes version this document — this is the largest such change to date, so
   the version bump and the migration table are the deliverable, not an
   afterthought.

2. **Adopt the Tasks extension for approval-gated operations.** Tasks — async
   operations with polling, mid-flight input, and a durable handle — is a
   near-exact match for the draft-approve interrupt. Express Attune's approval
   semantics in it: a capability invocation returns a task handle, the task
   enters an input-required state pending human approval, and the caller polls.
   This makes Attune's central safety property legible to any MCP host instead
   of being an internal detail. Note the parallel in A2A's eight-state task
   lifecycle (`input_required`, `auth_required`) and keep the vocabulary
   compatible.

3. **Expose Attune as an MCP server.** A **separate process** from the runtime
   holding user credentials — `CLAUDE.md`'s rule 5 and `design.md`'s principle 5
   are absolute: the credential-bearing runtime exposes no public listener. The
   server is a distinct service that talks to the runtime over the existing
   internal boundary, exactly as the stateless republisher and the hosted
   brokers already do. Tools to expose, read-only first:

   - `attune.brief` — today's assembled brief
   - `attune.what_matters` — the ranked correlation spine
   - `attune.importance` — the tier and reason for a sender (inspectable
     learning, which is the product's actual differentiator)
   - `attune.memory.search` — score-floored, provenance-framed
   - `attune.pending` — proposals awaiting decision
   - `attune.playbook.show` — the learned rules for a domain

   Then, gated and behind Tasks: `attune.propose` — request a capability from the
   registry, returning a task that requires human approval.

4. **Authorization is not optional.** OAuth 2.1 with Resource Indicators, per the
   spec's own recommendation. A calling agent is **not** the principal: it gets
   its own identity, its own allowlist, and its own audit actor type. Every
   invocation records an audit event naming the calling agent. A tool exposed
   over MCP grants no autonomy — `attune.propose` produces a proposal a human
   still approves, and the rung ceiling applies identically to a request that
   arrived over MCP.

5. **Consume meeting context over MCP rather than building capture.** Granola
   exposes an MCP server; Circleback and others are comparable. Add a meeting-
   context source through the existing `ingestion/sources.py` signal pipeline —
   read-only, triaged, into the attention store, no write path — so meeting
   content becomes an importance signal. **Do not build recording.** Otter's
   class action over recording without all-participant consent survived a
   standing challenge in early 2026, with a federal court finding non-consensual
   recording a concrete injury. Consuming someone else's consented capture is a
   materially different legal position from creating it.

6. **Record the decision to stay out of the browser.** Computer-use is table
   stakes elsewhere (Claude in Chrome GA on all paid plans; Gemini Spark drives
   Chrome with saved passwords) and Attune has none. The case against is strong
   and should be written down rather than left as an omission: Anthropic measures
   **11.2% residual prompt-injection success even with mitigations**, ChatGPT
   Atlas's deprecation cited injection and URL-handling leaks, and Attune's
   stated principle is that the model is not a security principal. Add a
   `docs/decisions.md` entry declining browser automation, with the conditions
   under which it would be revisited.

## Constraints

- **The runtime holding credentials exposes no public port.** Non-negotiable.
  The MCP server is a separate process with its own identity and no direct
  credential access.
- A calling agent is untrusted input, the same as an email body. Tool arguments
  are validated against a bounded contract; nothing an agent sends selects a
  tenant, an actor, or a rung.
- Read-only tools ship first and separately from `attune.propose`. Do not ship
  the write surface in the same change as the read surface.
- Each exposed tool needs a bounded, documented response shape — the same
  data-minimization discipline `hosted/google_provider.py` already applies
  (bounded response size, field stripping).
- Every new tool must be covered by the injection suite from prompt 27.

## Acceptance

- `docs/mcp-contract.md` at v2.0 with a client/server compatibility matrix, and
  client tests against fixtures for both the old and new envelope shapes during
  the deprecation window.
- A test asserting the client no longer sends a session header or handshake and
  still functions against a stateless fixture server.
- A test asserting an approval-gated `attune.propose` invocation returns a task
  in an input-required state and that no effect occurs until a human resolves it
  through the normal approval path.
- A test asserting a calling agent's identity appears as a distinct audit actor
  type, and that an agent outside the allowlist is refused.
- A test asserting the MCP server process holds no workspace, model, or memory
  credential.
- An injection test: a malicious tool argument attempting to raise a rung or
  select another actor is refused.
- `docs/decisions.md` entries for the spec migration, the server boundary, the
  meeting-context-over-MCP choice, and the declined browser automation.
