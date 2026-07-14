# Contributing

## Dev setup

```bash
python -m venv .venv && source .venv/bin/activate
./scripts/test.sh   # installs Attune editable and runs all tests
```

## Conventions

- The package at the repository root is independently buildable and testable.
- Model access stays provider-neutral: configure the official OpenAI SDK with
  a base URL, API key, and model identifiers. Do not add gateway clients or
  provider-specific catalogs.
- Keep the memory interface substrate-agnostic (`add` / `search` / `consolidate`)
  so the planned Mem0 → Graphiti migration is an implementation change, not an
  API change.

## Non-negotiable security rules

These follow directly from the OpenClaw failures surveyed in `docs/design.md` §8:

1. **Provenance tagging.** Any content fetched from email, chat, or the web is
   tagged untrusted before it reaches a model. Never concatenate untrusted
   content into an instruction context unlabeled.
2. **Scoped autonomy.** Autonomy is granted per `(action, domain)` via the
   permission matrix in `orchestrator/autonomy.py`, never globally. New
   autonomous behaviors require an explicit grant and should start no higher than
   rung 2 (propose, wait for approval).
3. **No inbound port on the credential-holding process.** Event ingestion is
   pull/outbound (Pub/Sub, Socket Mode); Calendar notifications and Google
   Chat interactions are isolated behind the credential-free republisher.
4. **Secrets never in code or logs.** Tokens come from env / secrets store and
   are never logged, even on error.
