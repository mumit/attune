# 08 — CLI: init wizard, doctor, run, brief

**Milestone:** M3 · **Depends on:** 05

---

Read `CLAUDE.md`, `docs/decisions.md`, `docs/roadmap.md` §1, and skim
`docs/deployment.md` (its manual steps are what `init`/`doctor` automate).
Run `pytest` before and after.

## Problem

Today the only entrypoint is `python -m aidedecamp` (which immediately needs
~25 env vars and live GCP), and setup is a 600-line manual runbook. There is
no way to try a single brief, validate a credential, or discover which of
the many settings is misconfigured other than reading tracebacks.

## Task

Build an `aidedecamp` console-script CLI (stdlib `argparse` — no click/typer
dependency) in `packages/aidedecamp/src/aidedecamp/cli/`, wired via
`[project.scripts]`. Subcommands:

1. **`aidedecamp init`** — interactive setup wizard writing `.env` (never
   committing it; refuse to overwrite without `--force`):
   - asks deployment (personal/telus), connector mode, ingestion mode
     (poll/push — poll is the default and the recommendation until prompt
     09's mode exists; if 09 hasn't landed, still write the setting and say
     push is currently required to run),
   - Fuel iX token (input hidden via `getpass`, written to `.env` only),
   - Google credentials: point at an existing JSON file, or walk the user
     through the OAuth user-credential bootstrap (run the local
     `InstalledAppFlow` consent flow from `google-auth-oauthlib` — add it to
     the `[google]` extra — and save the authorized-user file). Scopes from
     `credentials.SCOPES_DEFAULT`; **never** request `gmail.send` (rule 4).
   - optional Slack tokens / Chat space, brief time + timezone,
   - a single **data directory** (default `~/.aidedecamp/`): add
     `Settings.data_dir` (`ADC_DATA_DIR`) and make every existing `*_path`
     default derive from it (`<data_dir>/gmail_watch_state.json`, etc.)
     while still honoring explicit per-path overrides — this collapses six
     path vars into one for new users without breaking existing configs.
2. **`aidedecamp doctor`** — read-only validation, one PASS/FAIL/SKIP line
   per check with a *specific* fix hint: env parse, data dir writable,
   Fuel iX reachability (one cheap models/completions call; a 401 must
   surface `TokenRejectedError`'s "needs manual rotation" message — rule 6),
   Google credential load + a metadata-only Gmail and Calendar read, Mem0
   reachability, Slack `auth.test` when configured, Pub/Sub subscription
   existence when in push mode. Exit non-zero if any FAIL. Every check is an
   injected callable so tests fake them all.
3. **`aidedecamp brief`** — assemble one brief and print it to stdout
   (`--post` additionally posts to configured channels). The "try it in a
   terminal before wiring any chat app" moment.
4. **`aidedecamp run`** — `build_runtime().run()` after a fast doctor pass
   of the fatal checks (skippable with `--no-checks`).
5. Reserve `aidedecamp memory` and `aidedecamp autonomy` as subcommand
   groups that print "coming in M4" (prompts 11/12 fill them).

## Constraints

- Lazy-import heavy deps inside subcommands so `aidedecamp --help` works in
  a bare install.
- The wizard writes secrets only to `.env` (rule 6); nothing is echoed back.
- OAuth consent flow is user-initiated by `init`, runs on localhost, and is
  the one documented exception to "no inbound" — it's a short-lived local
  redirect listener during interactive setup, not a service port; note this
  explicitly in the decisions entry.

## Acceptance

- Offline tests: wizard writes expected `.env` from scripted answers (inject
  input/getpass); data-dir path derivation incl. explicit-override
  precedence; doctor renders PASS/FAIL and exit codes from faked checks;
  `brief` prints a fake-assembled brief. No test touches the network.
- README + `docs/deployment.md` gain a short "or just run `aidedecamp
  init`" pointer (full quickstart rewrite is prompt 10).
- `docs/decisions.md` entry (argparse-not-click, data_dir design, OAuth
  bootstrap exception) + CLAUDE.md module map.
