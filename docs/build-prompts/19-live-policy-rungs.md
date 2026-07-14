# 19 — Live policy evaluation + real rung semantics

**Milestone:** M6 stabilization · **Fixes review finding #2 (P0)**

---

Read `CLAUDE.md`, `docs/decisions.md` (autonomy entries), and the M6 section
of `docs/roadmap.md`. This touches the safety spine — re-read rule 3. Run
`pytest` before and after.

## Problem

1. **Revocations don't take effect until restart.** The matrix loads once in
   `build_app` and is captured by the compiled graph's closure. `attune
   autonomy revoke` writes the file; the running gate keeps the old object.
   A safety claw-back that waits for a restart isn't a claw-back.
2. **Rung semantics are half-built.** When the gate auto-applies
   (ACT_NOTIFY+), the dispatcher still posts an approval card and registers
   it pending — the user is asked to approve something already done, and
   the sweep later records a bogus IGNORED. And AUTONOMOUS behaves
   identically to ACT_NOTIFY: notify-after was never implemented.

## Task

1. **Matrix provider.** `build_draft_approve_graph` accepts
   `matrix_provider: Callable[[], PermissionMatrix]` (kept alongside
   `matrix` for back-compat; provider wins). The gate calls it per
   evaluation. `build_app` wires an mtime-checked loader over
   `JsonPermissionMatrixStore`: stat the file on each call, reload only on
   change, missing file → `default_matrix()`. `AppContext` gains
   `matrix_provider` (and `current_matrix()`); the chat `autonomy` command
   and the digest read through it so they show the live posture.
2. **Dispatcher branches on the interrupt.** In
   `handle_gmail_notification`, `_offer_resolution_hold`, and
   `run_follow_up_nudges`: `"__interrupt__" in result` → today's behavior
   (post card, register pending). Otherwise the graph auto-applied:
   - **ACT_NOTIFY** → no card, no pending entry; call the (new, optional)
     `notify` callable with an honest after-the-fact line ("Acted
     autonomously (act-notify grant): created draft for <subject>. Revoke
     with `attune autonomy revoke …`"), audit `auto_notified`.
   - **AUTONOMOUS** → no card, no notification; audit `auto_silent`.
   The rung comes from the result's `autonomy_gate` audit event
   (`max_rung`); add a small shared helper.
3. **Runtime** binds `notify` for the gmail path (it already has one for
   calendar) and passes it through followups/hold offers.

## Constraints

- The gate's fail-safe is untouched: no grant → interrupt, always. Provider
  failures (unreadable file) fall back to the LAST GOOD matrix, never to a
  more permissive one — log + audit the failure.
- No polling thread for reloads; stat-per-gate-evaluation is cheap and has
  no staleness window.

## Acceptance

- Tests: grant file changed on disk → next gate evaluation honors it (both
  directions — grant appears, revocation bites) with no rebuild; corrupt
  file mid-run → last good matrix + audited failure; auto-applied runs post
  NO card and register NOTHING pending; ACT_NOTIFY produces exactly one
  notify line; AUTONOMOUS produces none; interrupted runs unchanged.
- decisions.md entry + CLAUDE.md updates.
