# Deployment modes: which one do I run?

Attune ships as **one product** with **two deployment modes**. Both run the
same codebase (`src/attune/`), the same design principles
([`design.md`](design.md)), and the same normative security requirements
([`security-architecture.md`](security-architecture.md)) — they differ in who
runs the process and where your credentials and data live.

1. **Self-hosted, single-principal.** You install Attune on your own machine,
   home server, or VM, hold your own Workspace credential (or point it at an
   MCP server you trust), and run one instance for yourself. This is the full
   intelligence feature set — memory, drafts, autonomy, writes, everything in
   [`user-journey.md`](user-journey.md) sections 1–6 — and it is runnable
   today by following [`install/self-hosted.md`](install/self-hosted.md).
2. **Hosted multi-tenant service.** An operator runs Attune on Google Cloud
   for many customers; a customer just signs in with Google and never touches
   a terminal, Terraform, or a `.env` file. Per
   [`roadmap.md`](roadmap.md) and [`security-review.md`](security-review.md)
   §8, this is honestly a **development-stage platform**: most of it is
   "implemented and tested, not deployed" or "deployed in development with
   live evidence," gated behind default-off flags, and it is **not publicly
   operated** — production activation is explicitly blocked until every
   launch gate in `security-architecture.md` is evidenced. If you just want to
   use Attune yourself right now, this is not (yet) the mode for that; use
   self-hosted.

Everything below exists to help you pick the right mode and doc, not to
duplicate the instructions those docs already give.

## Modes at a glance

| Mode | Who it's for | What you get | Where your data lives | How you run it | Doc to follow |
|---|---|---|---|---|---|
| **Self-hosted, polling** (the default) | One person running Attune for themselves on a workstation, home server, or ordinary VM | The full intelligence set: brief, live Gmail/Calendar Q&A, memory, draft-and-approve, earned autonomy, optional Slack/Google Chat | Your `.env`, `ATTUNE_DATA_DIR` (SQLite/JSON/JSONL), local Qdrant | `attune init --quick` → `attune doctor` → `attune run` | [`install/self-hosted.md`](install/self-hosted.md) |
| **Self-hosted, Google Pub/Sub push** (advanced transport variant) | The same one person, wanting lower-latency Gmail/Calendar/Chat events instead of polling | The same feature set as polling self-hosted — this changes transport, not capability | Same as polling, plus a Compute Engine VM and a stateless Cloud Run republisher for Calendar/Chat callbacks | Everything in polling self-hosted, plus Pub/Sub topics/subscriptions and the republisher | [`deployment.md`](deployment.md) (§3–§11) |
| **Hosted multi-tenant, as a customer** | Someone who wants Attune without running anything themselves, once an operator has it running | Sign-in, Workspace connect/verify, a private-alpha read-only policy, optional Slack/Google Chat or a built-in browser conversation panel — today's gated feature slice, not the full self-hosted set | Tenant rows in the operator's Cloud SQL; nothing on your own machine | Sign in with Google in a browser — no install | [`user-journey.md`](user-journey.md) §0 |
| **Hosted multi-tenant, as the operator** | Whoever stands up and runs the multi-tenant service on GCP for others | A fleet of small Cloud Run services, forced-RLS PostgreSQL, and staged activation gates for every capability | Everything customer-facing lives in the operator's GCP project; the operator holds no per-customer OAuth in `.env` files | Terraform (foundation → data → runtime → edge), the migrator job, then ceremony-by-ceremony gate activation | [`install/hosted-operator.md`](install/hosted-operator.md) |

A fifth, even lighter option if you just want to see the code work without
any credentials at all:

| Mode | Who it's for | What you get | Where your data lives | How you run it | Doc to follow |
|---|---|---|---|---|---|
| **Try it in 10 minutes (dev loop)** | Someone evaluating the codebase before committing to either mode | The offline test suite — no live Gmail, Calendar, model, or channel calls | Nothing persists beyond the checkout | `pip install -e ".[dev]"` → `pytest -q` | This page, then [`install/self-hosted.md`](install/self-hosted.md) |

## Self-hosted, polling (the default)

### Who/why

One person, one instance, your own credentials. This is what most of the
documentation in this repository describes, and it's the only mode that is
fully built and runnable today. Start here unless you specifically need
lower-latency push transport or you are building the operated hosted service.

### Run it

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,orchestrator,memory,google,slack,mcp]"
cp .env.example .env
attune init --quick --recommended
```

`attune init --quick` asks only the essential questions (workspace backend,
data directory, mailbox, internal domains, LLM/embedding base URL, key, and
model) and prints the follow-up commands for channels and Google credentials;
`--recommended` fills in the documented mixed-provider model routing from
[`configuration.md`](configuration.md) as editable defaults. If you don't yet
have a Google OAuth client, run the guided checklist next:

```bash
attune init --google-setup
```

Then validate and go:

```bash
attune doctor
attune brief
attune run
```

To have `attune init` also provision and validate a local, loopback-only
Qdrant container instead of expecting one already running, add
`--target local` to any `attune init` invocation (it combines with `--quick`):

```bash
attune init --target local
```

The full manual walkthrough — Slack app creation, MCP as an alternative
Workspace backend, common failure fixes — is in
[`install/self-hosted.md`](install/self-hosted.md).

### What you get vs the other modes

The full intelligence set: durable memory, draft-and-approve workflows,
earned autonomy including sending and calendar writes (once explicitly
granted), and unrestricted connector read limits. Nothing here is
gated — every capability described in [`user-journey.md`](user-journey.md)
sections 1–6 is available once you configure it, no activation flags to flip.
The tradeoff is that you run and maintain the process yourself.

### Common confusions

- **Running self-hosted Attune on a cloud VM is still self-hosted mode**,
  not "hosted." "Hosted" in this repository always means the operated
  multi-tenant service in `src/attune/hosted/`. A single-principal instance
  on a GCE VM, a home server, or a laptop are all the same mode;
  [`deployment.md`](deployment.md) covers the VM case.
- **MCP vs `google_oauth` is a workspace-backend choice inside self-hosted,
  not a separate mode.** See the subsection below.
- **Slack and Google Chat are optional surfaces in every mode**, not modes
  of their own. You can run self-hosted with no channel at all (brief prints
  to the terminal), with one, or with both.

#### Workspace backend: `google_oauth` vs `mcp`

Both are ways to give self-hosted Attune Workspace access; neither is a
deployment mode. `google_oauth` (the default) is direct and well-supported,
and is required for the Pub/Sub transport variant below. `mcp` delegates
Google credentials, provider calls, and policy to a separate MCP server you
run or trust, and always uses polling — never Pub/Sub. Pick one per
[`install/self-hosted.md`](install/self-hosted.md)'s "Workspace access"
section; `attune doctor` validates whichever you chose.

## Self-hosted, Google Pub/Sub push (advanced transport variant)

### Who/why

The same single principal as polling self-hosted, but wanting lower-latency
Gmail/Calendar/Chat event delivery instead of a poll loop. This is **not** a
different product or a step toward "hosted" — it's the same self-hosted
runtime with Google Pub/Sub swapped in for polling, plus one small stateless
component (the republisher) to receive Calendar/Chat webhooks and hand them
to Pub/Sub. `design.md` calls this out explicitly: "Polling is the portable
default. `google_pubsub` explicitly names the advanced Google-specific
transport."

### What changes vs polling

- Ingestion mode: `ATTUNE_INGESTION_MODE=google_pubsub` instead of `poll`.
- You additionally provision Pub/Sub topics/subscriptions, a Compute Engine
  VM (or equivalent) to run `attune run`, and deploy `deploy/republisher` — a
  standalone Flask service with no model, memory, or user OAuth access — to
  Cloud Run to receive Calendar webhooks and optional Google Chat
  interactions.
- Workspace backend must be `google_oauth`; MCP is polling-only and does not
  support this variant.
- Everything else — memory, drafts, autonomy, Slack setup — is identical to
  polling self-hosted.

### Run it

Follow [`deployment.md`](deployment.md) sections 3–11 for the full runbook:
enabling GCP APIs, creating least-privilege service accounts, Pub/Sub
topics/subscriptions, deploying the republisher, creating the VM, and
materializing credentials into push-mode configuration. That guide is the
authority for the exact `gcloud` commands; this page won't duplicate them.

## Hosted multi-tenant, as a customer

### Who/why

Someone who wants to use Attune without running or maintaining anything —
once an operator has stood up the service and provisioned their tenant. This
mode is real code with live development evidence for much of the journey, but
it is **not publicly available**: today, an operator must bind your identity
to a tenant before your first sign-in works (production self-service signup
is designed in [`hosted-signup.md`](hosted-signup.md) but sits behind a
default-off gate, not yet activated).

### Run it

There is nothing to install. The journey is: sign in with Google → connect
Gmail/Calendar (a separate consent screen from sign-in) → the connection is
automatically verified → confirm the fixed private-alpha read-only policy →
optionally choose Slack and/or Google Chat for conversation and briefs (or
just use the built-in browser conversation panel, which needs no channel
install at all) → install/verify any chosen channel → activate. The complete,
step-by-step description of this journey — including exactly what each screen
shows and why — is [`user-journey.md`](user-journey.md) §0.

### What you get vs self-hosted

Today's hosted release is intentionally narrower than self-hosted: a fixed
R0 read-only policy (no sending, no calendar writes, no deletion or sharing),
capped live Gmail/Calendar reads, and a bounded five-way conversation router
(brief, Gmail, Calendar, general, or a structural write refusal). Draft-and-
approve, autonomy, and richer intelligence features exist as implemented-
but-not-deployed slices behind their own gates (see
[`roadmap.md`](roadmap.md) and [`security-review.md`](security-review.md)
§8) — they are not available to a hosted customer yet.

## Hosted multi-tenant, as the operator

### Who/why

Whoever provisions and runs the multi-tenant service on Google Cloud for
other people. This is a platform-engineering role, not an end-user path: end
users never run Terraform or hold a GCP role
([`hosted-gcp.md`](hosted-gcp.md) "Operator workflow").

### Run it

The deployment order is foundation → data → runtime → edge, gate by gate,
never all at once:

1. **Foundation** (`deploy/gcp/foundation`): private networking, IAM,
   KMS/CMEK, queues, audit retention. No customer data is allowed at this
   stage.
2. **Data** (`deploy/gcp/data`): the immutable migrator job applies
   checksum-pinned migrations, forces row-level security on every tenant
   table, and the boundary verifier confirms the exact privilege matrix.
3. **Runtime**: the dispatch broker, secret broker, workers, model gateway,
   channel broker, audit writer — each deployed dormant-first (feature flag
   off), then activated only after negative/adversarial tests pass.
4. **Edge**: the public control plane and provider ingresses, behind Cloud
   Armor rules admitting only exact paths, activated last per capability.

Every capability in the hosted service is behind its own default-off
activation gate, and activation is a ceremony — apply Terraform, verify an
empty plan and negative tests, *then* flip the flag and verify again — not a
single deploy. [`install/hosted-operator.md`](install/hosted-operator.md) is
the complete ordered runbook — prerequisites, Terraform apply order, the
migrator, service deployment, and every activation ceremony with its exact
flag names; `hosted-gcp.md` §"Deployment order and gates" is the architecture
reference it operationalizes, and `roadmap.md` tracks exactly which gates are
open in development today. **Successfully applying Terraform is not
successful onboarding**, and this platform is not publicly operated —
production activation requires every launch gate in
[`security-architecture.md`](security-architecture.md) to be evidenced first.

### What you're responsible for vs self-hosted

As operator you provision and evidence infrastructure, not per-customer
credentials — customers bring their own Google sign-in and Workspace
consent. You do not manage `.env` files per tenant; you manage Terraform
roots, migrations, and Cloud Armor rules. Every credential-bearing service
holds no public port; only the edge load balancer and verified provider
ingresses are public.

## Which state lives where (what do I back up, what do I delete)

This is a customer-experience summary of storage locations, not a security
boundary description — see [`data-lifecycle.md`](data-lifecycle.md) and
[`security-architecture.md`](security-architecture.md) for the normative
retention, export, and deletion contracts.

**Self-hosted:**

- `.env` (and, for direct OAuth, the authorized-user JSON it points at) —
  back this up like any other secret; losing it means reauthorizing Google
  and reconfiguring channels.
- `ATTUNE_DATA_DIR` — SQLite workflow/retry state, the JSONL audit log,
  polling cursors, grants, importance/attention files, setup state. Back
  this up together with Qdrant (see below); it's what `deployment.md`'s
  "Operations and maintenance" section calls out as the backup unit.
  Deleting it deletes your memory, audit history, and pending
  approvals — there is no server-side copy.
- Local Qdrant (the `deploy/compose.yml` volume) — your memory vectors.
  Back it up alongside `ATTUNE_DATA_DIR`; they must stay consistent with
  each other (a restored data dir with a stale Qdrant, or vice versa, is
  not a supported state).

**Hosted:**

- You hold nothing locally. Everything — account/tenant rows, connector
  credentials (encrypted), conversations, memory, policy, audit — lives in
  the operator's Cloud SQL and is subject to the operator's retention,
  export, and deletion ceremonies described in
  [`data-lifecycle.md`](data-lifecycle.md) and exercised through the
  product's own **Request export** and **Delete account** flows in
  [`user-journey.md`](user-journey.md) §6–§7 (both currently dormant behind
  their own activation gates, per that document).
- "Backing up" hosted state is the operator's job, not yours; from a
  customer seat, the only actions available are export and delete, both
  gated on recent sign-in.
