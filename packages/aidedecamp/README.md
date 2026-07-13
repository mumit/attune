# aidedecamp

A self-learning workspace assistant over Gmail, Calendar, Google Chat, and Slack,
running on Fuel iX. Slack is the supported first live text channel; voice is a
future phase and Google Chat app authentication is not production-wired yet.

This is the application package. The generic Fuel iX transport lives in the
sibling [`bearer-openai`](../bearer-openai) package.

See [`../../docs/design.md`](../../docs/design.md) for the full architecture,
memory design, autonomy model, and roadmap;
[`../../docs/decisions.md`](../../docs/decisions.md) for the settled
architectural decisions and why;
[`../../docs/getting-started.md`](../../docs/getting-started.md) for the guided
personal setup; and
[`../../docs/deployment.md`](../../docs/deployment.md) for how to actually run
this against a live GCP project.

## Layout

```
src/aidedecamp/
  fuelix.py       Fuel iX base URL, verified model IDs, task-shape routing
  config/         per-deployment settings (personal vs TELUS) from env
  credentials.py  Google credential loading (service account / OAuth user / ADC)
  orchestrator/   LangGraph draft-and-approve graph, autonomy permission matrix,
                  triage (plain fn, Task.CLASSIFY), scheduling conflict detection
                  (plain fn), the shared resume_workflow() Command(resume=...)
  memory/         substrate-agnostic MemoryStore, Mem0 impl, capture signals
  connectors/     Workspace boundary; direct OAuth is production-wired, while
                  MCP currently requires an injected transport
  ingestion/      Gmail watch/history, Calendar watch/sync, Chat Workspace
                  Events + card-interaction decoding
  dispatcher.py   the routing seam: notification/event -> graph invocation,
                  conflict-check, or brief/converse reply
  channels/       Slack (Socket Mode) + Google Chat (Cards v2) — thin doors,
                  no assistant logic
  brief.py        read-only morning brief (plain fn, no HITL need)
  app.py          build_app() -> AppContext (graph + memory + client + audit log)
  runtime.py      build_runtime() -> Runtime, the always-on entrypoint
  audit/          structured, queryable reason-for-action log (JsonlAuditLog)

deploy/
  compose.yml        canonical Qdrant + optional assistant stack
  republisher/       standalone Cloud Run service (own deps, own tests) —
                     the two webhook exceptions to "no inbound port": Calendar
                     push notifications and Chat card-interactions
```

## Status

Read-only and rung-2 (propose, wait for human approval) are built end to end
and tested (offline — no live credentials needed to run the suite): Fuel iX
routing, per-deployment config, the autonomy matrix, the LangGraph
draft-and-approve graph, Mem0-backed memory, triage, Gmail + Calendar + Google
Chat + Slack ingestion and channels (including Slack conversational Q&A and
Google Chat's async card-interaction flow), Calendar scheduling-conflict
detection and approved tentative holds, the audit log, and `runtime.py` wiring
everything into one process.

The conflict-triggered Calendar hold action is built at PROPOSE. Not built:
invite responses, rescheduling, a production Chat app-auth credential, and a
complete live deployment (credentials and one terminal brief have been
exercised, but not the always-on approval loop — see
`../../docs/deployment.md`). See `../../CLAUDE.md`'s "Next steps" for the
current, maintained list.

## License

MIT
