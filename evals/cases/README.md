# Captured regression cases

Files here are written by `attune eval capture` (see
`src/attune/evals/capture.py`) — one JSON file per decided (edited or
rejected) proposal in the local decision ledger, named `<proposal_id>.json`.

Every case is redacted (`src/attune/evals/schema.py::redact`) before being
written, but redaction is best-effort (emails/URLs/phone numbers only, no
name detection). **Review a case's `inputs`/`proposed_text`/`gold_text`
before committing it** — this directory is meant to be a reviewable, opt-in
regression set, never an automatic harvest of a principal's mail.

Capture is manual and local: nothing in this codebase writes here except
that one explicit CLI command.
