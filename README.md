# Aide-de-camp

A self-learning workspace assistant over Gmail, Calendar, Google Chat, and Slack,
running on [Fuel iX](https://fuelix.ai), with a Slack text interface today. It gets
better at being *your* assistant over time: it learns your preferences from the
edits you make to its drafts, remembers who and what your projects are about, and
earns autonomy one narrow, reversible action at a time rather than being handed
it up front.

Read [`docs/design.md`](docs/design.md) first — it's the source of truth for the
architecture, the memory model, the earned-autonomy ladder, and the phased
roadmap. [`docs/decisions.md`](docs/decisions.md) is a running log of settled
architectural decisions and the reasoning behind them.
[`docs/deployment.md`](docs/deployment.md) covers the concrete GCP setup for
running it.

## Why a monorepo with two packages

```
packages/
  bearer-openai/   Generic, vendor-neutral OpenAI-compatible client for
                   bearer-token gateways. No Fuel iX (or any vendor) specifics.
                   Independently publishable and reusable by anyone behind such
                   a gateway. Intended to be split into its own repo later.

  aidedecamp/      The assistant itself. Depends on bearer-openai. Carries all
                   the Fuel iX config, orchestration, memory, connectors, and
                   channels.
```

The two are developed together now for convenience; `bearer-openai` deliberately
knows nothing about `aidedecamp` so it can leave home cleanly.

## Quickstart — first brief in about 15 minutes

Prerequisites: Python 3.10+, Docker, a Google account, a Google Cloud project
with Gmail and Calendar APIs enabled, an OAuth desktop-client JSON, and a Fuel
iX bearer token. The default **poll mode** needs no Pub/Sub, VM, Cloud Run, or
webhook infrastructure — everything is outbound-only.

```bash
# 1. Clone and install
git clone <this repo> && cd aidedecamp
python -m venv .venv && source .venv/bin/activate
pip install -e "packages/bearer-openai" \
            -e "packages/aidedecamp[orchestrator,memory,google,slack]"

# 2. Start the memory substrate (Qdrant; Mem0 runs in-process)
docker compose -f packages/aidedecamp/deploy/compose.yml up -d

# 3. Interactive setup — writes and subsequently auto-loads .env; point it at
#    the OAuth client JSON and let it run the Google consent flow
aidedecamp init

# 4. Validate everything, then see your first brief in the terminal
aidedecamp doctor
aidedecamp brief
```

Make it always-on with `aidedecamp run` (a terminal, tmux, or the systemd
unit in [`docs/deployment.md`](docs/deployment.md)) — or fully containerized:

```bash
docker compose -f packages/aidedecamp/deploy/compose.yml --profile assistant up -d --build
```

From there it polls your inbox and calendar, posts a morning brief at your
configured time, and sends draft-approval cards to Slack; approving one creates
the draft in Gmail for you to send. Never commit `.env`. Follow the exact
[Google OAuth](docs/deployment.md#4-google-workspace-access-and-oauth) and
[Slack app](docs/deployment.md#11-slack-app-setup) steps before running the
wizard.

### Dev setup

```bash
pip install -e "packages/bearer-openai[dev]" -e "packages/aidedecamp[dev]"
pytest packages/aidedecamp packages/bearer-openai
```

Optional extras (the package imports without them): `[memory]` (Mem0 +
Qdrant), `[orchestrator]` (LangGraph), `[slack]` (Slack Bolt), `[google]`
(direct-OAuth Google API access + Pub/Sub).

`packages/aidedecamp/deploy/` holds standalone deployable infrastructure —
the compose stack, the assistant Dockerfile, and the Calendar-webhook/
Chat-interaction republisher service — each with its own dependency set, not
part of the main test run (see `pytest.ini`'s `norecursedirs`).

## Running it for real

Poll mode (above) is the day-one path. The hardened production posture —
Pub/Sub push ingestion, the republisher on Cloud Run, Secret Manager, a
dedicated GCP project per deployment — is Track B in
[`docs/deployment.md`](docs/deployment.md), for both a personal and a
TELUS-style deployment. `aidedecamp doctor` tells you what's missing at
each step.

## Status

Everything through the 2026-07 roadmap (`docs/roadmap.md`, all 23 build
prompts — including the M6 stabilization milestone from an independent
external review) is built and tested — 565 offline tests, no live
credentials needed for the suite:

- **The full interaction loop**: triage (memory-informed) → draft → approval
  card → approve/edit/reject — approving creates the
  Gmail draft, editing captures the correction diff as a learning signal,
  ignoring decays into a signal too. Follow-up nudges for quiet threads and
  conflict-triggered calendar hold proposals ride the same loop. Slack is the
  live-ready surface; Google Chat's equivalent is implemented and tested
  offline but still needs a separate app-auth credential.
- **It runs itself**: scheduler (brief, renewals, sweeps, consolidation,
  weekly autonomy digest), supervised ingestion loops with backoff and
  heartbeats, structured logging. Poll mode (default) needs no GCP runtime
  infrastructure; push mode is the hardened posture.
- **Learning you can see and steer**: `aidedecamp memory list/forget/
  remember` (and the same in chat), a persisted autonomy matrix with
  `aidedecamp autonomy grant/revoke/record` and audit-derived graduation
  suggestions, a real nightly consolidation pass, and a memory-quality
  regression set (design §2.4).
- **Setup in ~15 minutes**: `aidedecamp init` wizard, `doctor` validation,
  the compose stack, and the quickstart above.
- **Hardened for real accounts (M6)**: deny-by-default human allowlists on
  every channel surface, email-safe ingestion and reply envelopes, live
  policy (revocations bite without a restart) with real rung semantics, a
  production-verified audit pipeline, staleness-refusing approvals,
  verified memory consolidation, and calendar bootstrap suppression.

What's deliberately not built: a production-wired Google Chat app-auth
credential (so live Cards v2 remain deferred), invite accept/decline and rescheduling (each
needs its own decisions entry first — see `docs/decisions.md`, "Calendar
write actions"), the Graphiti migration, the browser surface, and voice
(design.md phases 4–7). **Nothing has run against a live account yet** —
the Phase-0 "a genuinely useful brief for a week without babysitting" bar
is unverified until someone deploys it. See `CLAUDE.md`'s "Next steps".

## Security posture (read before running anything that touches real data)

This project is, by construction, the exact shape the OpenClaw incidents warned
about: a privileged agent exposed to untrusted input (any email you receive) with
the ability to act. The design defends against that deliberately — see
`docs/design.md` §3.2 and §8. Rules that are non-negotiable from day one (the
full list is in `CLAUDE.md`):

- Untrusted content (email/chat bodies) is tagged as untrusted before it
  reaches the model — never framed as instructions.
- Autonomy is scoped per `(action, domain)`, never global, and fails safe to
  human approval.
- Send is refused by default; enabling it is a deliberate, separately-reviewed
  change.
- No inbound port on the credential-holding process — ingestion is
  pull/outbound (Pub/Sub, Slack Socket Mode); the two sources needing a real
  webhook (Calendar, Chat card-interactions) go through a separate,
  credential-free republisher service that only forwards to Pub/Sub.

Do not short-circuit any of these to make something "work."

## License

MIT — see [`LICENSE`](LICENSE).
