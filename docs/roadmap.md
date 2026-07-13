# Roadmap v2 — from "code-complete" to "assistant I actually rely on"

*Written 2026-07 after a full design + implementation review (312 tests passing
at time of writing). This supersedes the phase list in `design.md` §6 as the
working plan — the phases there remain the long-term arc; this document is the
concrete, ordered execution plan for what's next, with ready-to-run build
prompts in `docs/build-prompts/`.*

> **Execution status (2026-07): prompts 01–16 are implemented** — one commit
> per prompt, 504 tests passing (from the 312 baseline), every defect in §1's
> table closed, each prompt's decisions recorded in `docs/decisions.md`.
>
> **Then an independent review (GPT-5.6 Sol) found 8 cross-cutting defects**
> the prompt-by-prompt strategy missed — production-path issues at the joints
> between prompts (identity, rung semantics, the audit pipeline, email
> envelopes). All 8 verified against this codebase and mapped to **M6 below
> (prompts 17–23)**.
>
> **M6 is implemented** — one commit per prompt, 551 tests passing, all 8
> findings closed, decisions recorded per prompt. The
> no-write-actions-before-M6 rule is therefore lifted; the shadow-deployment
> sequence still applies: read-only first, then PROPOSE, and keep ACT_NOTIFY
> grants off until the audit trail proves itself live. Next: deploy (Track A
> in `docs/deployment.md`), hold the Phase-0 bar, then the design.md phase
> 4–7 tail.
>
> **Post-M6 hardening is implemented** — seven follow-up architecture findings
> now enforce private proactive destinations, durable cursor-consumed retries,
> atomic single-use approvals, execution-safe compatibility checks, fail-closed
> policy reloads, canonical cross-channel principal identity, and successful
> apply evidence before graduation. The core suite now has 571 passing tests.
>
> **Deployment audit note:** completion here means offline implementation,
> not live readiness for every surface. Slack + per-user Google OAuth is the
> supported first deployment. Google Chat Cards v2 still need a separately
> wired app-auth credential; see `docs/deployment.md` §12.

---

## 1. Review summary

### What's genuinely strong (keep, don't churn)

- **The safety architecture is real, not aspirational.** The autonomy matrix
  fails safe, send is structurally refused, provenance tagging is enforced at
  every prompt boundary, and the no-inbound-port rule survived the one case
  (Chat card clicks) where it was genuinely inconvenient. This is the part of
  the codebase that must not be weakened while fixing anything below.
- **Testability discipline.** Every collaborator injected, the whole suite runs
  offline in ~1s. New work should keep this bar.
- **The decision log.** `docs/decisions.md` captures *why*, which is rarer and
  more valuable than the code itself.

### The honest user-perspective assessment

Judged as *code*, this is in excellent shape. Judged as *a personal assistant a
user sets up and relies on*, three things are true today:

1. **The interaction loop is open at both ends.** Clicking **Approve** on a
   draft card captures a memory signal and… nothing else. No Gmail draft is
   created (`connector.create_draft` has zero callers outside the connector
   layer); the Slack confirmation even says "✅ Approved — sending," which is
   false. Clicking **Edit** — the single richest learning signal in the whole
   design (§2.2) — is a `pass` stub in Slack and an unwired dialog in Chat.
   So the learning loop can only ever see approve/reject, and approval itself
   delivers no value beyond the text already visible on the card.
2. **It cannot actually run unattended.** `Runtime.run()` starts the pull
   loops but nothing ever calls `renew_gmail_watch` / `renew_chat_subscription`
   / `renew_calendar_watch` — watches silently lapse within 7 days. Nothing
   schedules the morning brief (the Phase-0 deliverable). Nothing ever calls
   `consolidate()`. And every pull loop runs in a daemon thread with no
   exception handling: one transient network error kills that ingestion source
   silently until a manual restart.
3. **Setup is a weekend of GCP plumbing.** The deployment guide is a 600-line
   manual runbook: a GCP project, five enabled APIs, OAuth consent, four
   Pub/Sub topic+subscription pairs, a Cloud Run republisher, Secret Manager,
   a systemd unit, and ~25 env vars — *before the first brief ever appears*.
   For a project whose design philosophy is "memory is the product," the
   product is currently gated behind infrastructure that most of it doesn't
   need on day one (polling Gmail every two minutes is outbound-only,
   rule-5-clean, and needs zero Pub/Sub).

### Specific defects found (fixed by the milestones below)

| # | Finding | Where | Severity |
|---|---|---|---|
| 1 | Approved/edited drafts are never materialized into Gmail; no `create_draft` caller | `orchestrator/draft_approve.py` (no apply node), `dispatcher.py` | Value blocker |
| 2 | Slack "✅ Approved — sending." message is untrue (nothing sends, nothing is even drafted) | `channels/slack.py` `_approve` | Trust bug |
| 3 | Edit flow unreachable: Slack modal is a `pass` stub; Chat dialog submit unwired → `capture_correction` can never fire in production | `channels/slack.py` `_edit`, `channels/gchat.py` | Value blocker |
| 4 | `ActionSignal.IGNORED` defined, never captured — no notion of a pending card that expired | `memory/signals.py`, no pending-approval tracking anywhere | Learning gap |
| 5 | No scheduler: watch renewals never invoked from `run()`, brief never posted, consolidation never runs | `runtime.py` | Deployment blocker |
| 6 | Pull loops die silently on any exception (daemon threads, no try/except, no backoff, no restart) | `runtime.py` `run_*_pubsub_loop` | Deployment blocker |
| 7 | Brief computes "today" in UTC (`now.replace(hour=0…)` on a UTC now) and prints event times in UTC — wrong day boundary and wrong clock for any non-UTC user | `brief.py` | Correctness |
| 8 | `_converse` is one-shot: no conversation history, so follow-up questions ("what about the second one?") can't work | `dispatcher.py` | UX gap |
| 9 | Permission matrix is code-only: no persistence, no way to grant/revoke without editing source, no track record → "autonomy is earned" has no earning mechanism | `orchestrator/autonomy.py` | Design promise unmet |
| 10 | Memory is write-only from the user's view: `get_all`/`delete` exist on the store but no surface exposes them ("what do you know about me?" / "forget that") | `memory/`, channels | Design promise unmet (§3.1 browser row, Phase 5) |
| 11 | Duplicate approval cards possible: each Gmail notification for the same thread starts a fresh workflow + card; nothing tracks an already-pending approval per Gmail thread | `dispatcher.handle_gmail_notification` | UX gap |
| 12 | Triage ignores memory ("your past reactions", design §1.2) — known fast-follow, still open | `orchestrator/triage.py` | Learning gap |

---

## 2. The plan — five milestones, ordered by user value per unit effort

Each milestone has a "you can feel it" bar, mirroring design §6's "use it daily
before moving on" discipline. Build prompts (self-contained, Sonnet-ready) live
in `docs/build-prompts/`, numbered to match.

### M1 — Close the interaction loop *(prompts 01–04)*

The assistant becomes worth clicking on: Approve creates a real Gmail draft
(and says so honestly), Edit works end-to-end on both channels (correction
capture finally fires), ignored cards decay into IGNORED signals, and Q&A can
handle a follow-up question.

- **01** Apply node: materialize approved/edited drafts via
  `connector.create_draft`; honest confirmations.
- **02** Edit flow: Slack modal + Chat dialog, wired to `resume("edited", text)`.
- **03** Pending-approvals registry: dedupe cards per Gmail thread; expiry →
  `ActionSignal.IGNORED`.
- **04** Conversation context: short rolling per-user window for `_converse`.

**Felt as:** "I approved a reply from Slack and the draft was waiting in Gmail."

### M2 — It runs itself *(prompts 05–07)*

The always-on process actually stays on: an internal scheduler posts the
morning brief at a configured local time, renews all three watches daily, and
triggers the nightly consolidation hook; pull loops survive errors with
backoff and restart; the brief understands the user's timezone and gets prep
notes per meeting.

- **05** Scheduler: in-process, injected-clock-testable; brief + renewals +
  consolidation cadence.
- **06** Loop supervision + structured logging: no silent thread death, a
  heartbeat you can grep.
- **07** Brief v2: local-timezone day boundaries and times; per-meeting prep
  from the latest related thread; quiet-thread section.

**Felt as:** the Phase-0 bar — a genuinely useful brief every morning for a
week with zero babysitting.

### M3 — Easy to set up *(prompts 08–10)*

An evening, not a weekend: a real CLI (`aidedecamp init` wizard, `doctor`,
`run`, `brief`), a **polling mode** that needs no Pub/Sub / republisher /
Cloud Run at all (outbound-only, rule-5-clean — push infra becomes an
optimization you graduate to), one data directory instead of five path vars,
and a single `docker compose up` for the full stack.

- **08** CLI: `init` (interactive setup + OAuth bootstrap), `doctor`
  (validates every credential/scope/resource with actionable errors), `run`,
  `brief`, plus `memory`/`autonomy` subcommand stubs for M4.
- **09** Polling ingestion mode: Gmail history + Calendar sync-token + Chat
  spaces polling behind the same dispatcher seam; `ADC_INGESTION_MODE=poll|push`.
- **10** Compose + quickstart: full-stack `docker-compose.yml`, a 15-minute
  README quickstart, deployment.md restructured as "quickstart vs. hardened".

**Felt as:** "I went from git clone to my first brief in under an hour."

### M4 — Learning you can see and steer *(prompts 11–14)*

The design's actual differentiators: memory transparency ("what do you know
about me?" / "forget that" / "remember X" in chat), a persisted permission
matrix with grant/revoke commands and an audit-derived track record that
*suggests* graduations ("15/15 spam-invite declines approved — graduate to
act-and-notify?"), a real consolidation pass with a memory-quality regression
set (design §2.4), and memory-informed triage.

- **11** Memory transparency commands (both channels + CLI).
- **12** Autonomy persistence + grant/revoke + graduation suggestions —
  suggestions only; a human always makes the grant (rule 3).
- **13** Consolidation pass + LoCoMo-style memory eval set.
- **14** Memory-informed triage (past reactions to this sender/topic).

**Felt as:** "I corrected it once, saw the memory it formed, and the next
draft was already different."

### M5 — Proactive value *(prompts 15–16, then design.md phases 5–7)*

- **15** Quiet-thread nudges (design §3.3): "no reply from Marcus in 4 days —
  want a follow-up drafted?"
- **16** Calendar write-action layer, design-first: the deliberately-deferred
  hold-creation/invite-response flow, entering at PROPOSE with its own
  autonomy-gate review (the trigger question gets answered in a decisions.md
  entry before code).
- Then, per the original arc: Graphiti migration (design Phase 4 tail),
  browser audit/correct surface (Phase 5), voice (Phase 6), presence-aware
  routing (Phase 7).

### M6 — Stabilization *(prompts 17–23; from the 2026-07 external review)*

An independent review of the completed M1–M5 work found defects that unit
tests against fakes structurally cannot catch — each prompt's seams were
tested, the joints between them were not. Verified findings → fixes:

| # | Prompt | Fixes | Review finding |
|---|---|---|---|
| 17 | `17-principal-allowlist.md` | Deny-by-default human allowlists on every Slack/Chat surface; actor-bound resumes | #1 (P0): transport authenticated, human never |
| 18 | `18-email-safety.md` | SENT/DRAFT filtering; reply envelope = latest inbound sender + Reply-To; never draft to the owner | #3 (P0): reacts to own mail; follow-ups addressed to the owner |
| 19 | `19-live-policy-rungs.md` | mtime-reloaded matrix at the gate; interrupt-branching (no phantom cards); ACT_NOTIFY notifies-after, AUTONOMOUS silent | #2 (P0): revocations need a restart; rung semantics half-built |
| 20 | `20-resume-audit.md` | resume_workflow writes post-resume events (correct domain, actor); end-to-end pipeline test, zero synthetic entries | #4 (P1): graduation evidence never captured in production |
| 21 | `21-freshness-idempotency.md` | Retry-then-audit fetches; source-snapshot freshness check at apply; honest sweep status | #5+#6 (P1): silent loss; stale cards act on changed sources |
| 22 | `22-verified-consolidation.md` | Verify add() before any delete; abort batch on write failure; journal every mutation | #7 (P1): consolidation can erase evidence |
| 23 | `23-calendar-bootstrap.md` | Rebaseline without dispatching; symmetric-pair dedupe; per-run offer cap | #8 (P2): bootstrap flood of hold proposals |

The reviewer's fuller "action kernel" proposal (transactional inbox/outbox,
single-use approval tokens, principal tables in SQLite WAL) is recorded as an
option in the M6 decisions entry rather than adopted: for a single-principal
personal deployment, these seven fixes land the same guarantees inside the
existing seams. **Tripwire for revisiting**: multi-user use, or running
ACT_NOTIFY grants in production at meaningful volume.

**Felt as:** "a stranger in my Slack workspace gets politely refused, a
revocation bites immediately, and a week-old card can't touch a thread that
moved on."

---

## 3. Sequencing notes

- **M1 before M2**: a scheduler that faithfully posts cards nobody can act on
  compounds the trust bug; close the loop first.
- **09 (polling) is the single highest-leverage setup change** — it deletes
  four Pub/Sub pairs, the republisher, and Cloud Run from the day-one path.
  Push mode stays fully supported and remains the recommended production
  posture; `doctor` tells you when you're ready to graduate.
- **Prompt 03 depends on 05** for its expiry sweep (the registry ships with a
  `sweep()` the scheduler calls); build 03's capture logic anyway and wire the
  cadence when 05 lands.
- Nothing in M1–M4 touches the six non-negotiable rules in `CLAUDE.md`; every
  prompt restates the rules it brushes against.
