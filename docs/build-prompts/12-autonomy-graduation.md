# 12 — Autonomy: persistence, grant/revoke, and earned graduation

**Milestone:** M4 · **Depends on:** 08, 11 ·
**Fixes roadmap defect #9**

---

Read `CLAUDE.md`, `docs/decisions.md`, and `docs/roadmap.md` §1. This prompt
touches the project's central safety primitive — re-read rule 3 and
`orchestrator/autonomy.py`'s docstring before writing a line. Run `pytest`
before and after.

## Problem

"Autonomy is earned, not granted" is the design's second pillar, but the
earning mechanism doesn't exist: `default_matrix()` is hardcoded, grants
survive neither a restart nor exist anywhere a user could make one, there's
no way to *see* the current posture, and nothing computes the track record
("15/15 approvals on this action — ready to graduate?") that the whole
ladder concept depends on. The audit log already records every
`autonomy_gate` and `human_decision` — the raw material is there.

## Task

1. **Persistence.** `JsonPermissionMatrixStore(path)` (the
   `ingestion/state.py` pattern): loads/saves grants as
   `{"action|domain": rung}` JSON. `Settings.autonomy_state_path`
   (`ATTUNE_AUTONOMY_STATE_PATH`, data-dir-derived). `app.build_app` loads the
   persisted matrix when present, else `default_matrix()`; the file is only
   ever written through the explicit grant/revoke operations below — the
   matrix object itself stays frozen/immutable.
2. **Operations module** `orchestrator/grants.py`:
   - `grant(store, matrix, action, domain, rung, *, audit_log, user_id)` /
     `revoke(...)` — validate enum members, persist, audit
     (`autonomy_granted`/`autonomy_revoked` under an `"autonomy"` workflow —
     these are the most audit-worthy events in the system).
   - `show_matrix(matrix)` — human-readable posture table.
   - `track_record(audit_log, *, action, domain, window_days=30)` — fold
     the audit log's `autonomy_gate` + `human_decision` (+ prompt 03's
     ignored signals) into counts: approved-unedited / edited / rejected /
     ignored, and a `GraduationSuggestion` when a configurable bar is met
     (default: ≥10 decisions, ≥95% approved-unedited, 0 rejections in
     window). Suggestions are *information only*.
3. **Surfaces.** CLI `attune autonomy` group: `show`, `grant <action>
   <domain> <rung>`, `revoke <action> <domain>`, `record [<action>
   <domain>]`. Chat commands (extending prompt 11's router): `autonomy` →
   show + any current suggestions; grant/revoke stay **CLI-only** for now —
   a chat channel that relays untrusted content must not be able to escalate
   autonomy via a spoofed-looking message; state this in the docstring and
   decisions entry.
4. **Weekly digest job** (scheduler, prompt 05): post track-record
   summaries + suggestions to the default channel ("You approved all 12
   spam-invite drafts unedited this month. To graduate this to
   act-and-notify, run: `attune autonomy grant …`").

## Constraints (this is the safety spine — no shortcuts)

- **A human always makes the grant.** No code path may auto-apply a
  suggestion, however strong the record. The gate's fail-safe default
  (absent grant → human approval) is untouched.
- Granting `SEND_REPLY` at any rung must additionally warn that send is
  structurally disabled without `send_enabled` + scope (rule 4) — the grant
  alone must not be sufficient, and a test must prove the existing
  `SendNotPermitted` still fires with a grant present.
- Rung/action/domain parsing is strict enum validation — a typo must error,
  never silently default.

## Acceptance

- Offline tests: persistence round-trip through `build_app`'s load path;
  grant/revoke audit + immutability (new matrix object, old unchanged);
  track-record math over synthetic audit files incl. the suggestion bar
  edges; the send-gate-survives-grant test above; CLI + chat surfaces.
- `docs/decisions.md` entry (graduation bar, CLI-only-grants rationale,
  suggestion-never-auto-applied) + CLAUDE.md updates.
