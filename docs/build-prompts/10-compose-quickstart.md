# 10 — Compose stack + 15-minute quickstart

**Milestone:** M3 · **Depends on:** 08, 09

---

Read `CLAUDE.md`, `docs/decisions.md`, `docs/roadmap.md`, and the current
`docs/deployment.md` and READMEs. Run `pytest` before and after (this prompt
is mostly packaging/docs; any code it touches still needs tests).

## Problem

Even with the CLI and polling mode, a new user still has to discover and
start Qdrant + Mem0 themselves (`deploy/mem0-compose.yml` covers only part
of the stack), then read a deployment guide whose first page is GCP project
creation. The happy path deserves to be: clone → compose up → `attune
init` → `attune brief`.

## Task

1. **`deploy/compose.yml`** — the full local/VM stack: Qdrant, Mem0, and the
   assistant itself (a new `Dockerfile` for the attune package,
   `python -m attune` entrypoint, `.env` file passed via `env_file`,
   data dir + state mounted as a named volume). The assistant service is
   optional-profile (`--profile assistant`) so a user can also run just the
   memory substrate and use the CLI on the host during setup. Keep
   `deploy/mem0-compose.yml` as a thin pointer or fold it in — pick one,
   note it in the decisions entry.
2. **Root `README.md` quickstart rewrite** — a genuinely ~15-minute path:
   prerequisites (Docker, a Google account, a configured OpenAI-compatible gateway token), four steps
   (clone, `docker compose up -d` for the substrate, `pip install -e` +
   `attune init`, `attune brief`), then "make it always-on"
   (`attune run` under the compose assistant profile or systemd), then
   pointers: hardened GCP/push deployment → `docs/deployment.md`, design →
   `docs/design.md`, roadmap → `docs/roadmap.md`.
3. **`docs/deployment.md` restructure** into two clearly labeled tracks:
   *Track A — quickstart/poll mode* (short, references the CLI, no Pub/Sub,
   no republisher) and *Track B — hardened push deployment* (the existing
   content, updated for anything prompts 01–09 changed: new env vars,
   scheduler, supervision, CLI bootstrap replacing manual first-run steps).
   Keep the existing honesty convention: steps that have never been run
   against a live project stay explicitly flagged as unexercised.
4. **`.env.example` refresh** — every setting added since it was written
   (timezone, brief time, ingestion mode, poll seconds, data dir,
   conversation/pending paths, log level…), grouped and commented, poll-mode
   defaults first.

## Constraints

- The Dockerfile installs only `[orchestrator,memory,google,slack]` extras —
  the republisher keeps its own image and its own directory (standalone
  deployable convention in CLAUDE.md).
- No secrets in images or compose files (rule 6): `.env` via `env_file`,
  documented as gitignored.
- Docs must not oversell: the Phase-0 "week without babysitting" bar is
  still unverified until someone actually runs it — keep that framing.

## Acceptance

- `docker compose -f deploy/compose.yml config` validates in CI-less local
  check; the attune image builds (`docker build`) — include both as a
  documented manual verification if Docker isn't available in the test env.
- A fresh reader can follow README top-to-bottom with no forward references
  to unexplained env vars.
- `docs/decisions.md` entry + CLAUDE.md environment section updated.
