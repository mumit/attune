# Deployment

Attune is a portable, long-running Python service. A deployment is one instance
for one principal, with its own credentials, memory, state, and audit log. Run
multiple isolated instances when serving multiple principals; this is an
operational boundary, not a named application mode.

## Recommended path

1. Start in polling mode on a VM, container host, or workstation.
2. Mount a durable `ATTUNE_DATA_DIR` and Qdrant volume.
3. Inject `.env` values from the platform's secret/configuration service.
4. Run `attune doctor` as a preflight and `attune run` under a service manager.
5. Back up state and audit data; rotate credentials independently.

The included Compose stack starts Qdrant and can optionally run Attune:

```bash
docker compose -f deploy/compose.yml up -d
docker compose -f deploy/compose.yml --profile assistant up -d --build
```

## Workspace backends

Direct Google OAuth is the default and supports polling plus Google Pub/Sub.
MCP is useful where a managed server should own credentials, consent, tool
policy, and audit controls. Configure Streamable HTTP endpoints and use polling.
MCP is an architectural boundary—not inherently a richer source of Gmail or
Calendar capability—so use it when that boundary has operational value.

## Google Pub/Sub transport

Set `ATTUNE_INGESTION_MODE=google_pubsub` only for the Google OAuth backend.
Provision topics and pull subscriptions for Gmail, Chat Workspace Events, Chat
app interactions, and Calendar as needed. The credential-holding Attune process
only makes outbound pull requests.

Calendar webhooks and Google Chat app callbacks require synchronous public HTTP
responses. Deploy `deploy/republisher` as a small stateless
service. It holds no user OAuth, model credential, memory, or workflow state. It
verifies Google Chat requests, answers dialog-open actions synchronously, and
publishes Chat messages/card decisions and Calendar change signals to Pub/Sub.

Required republisher variables include `CALENDAR_PUBSUB_TOPIC`,
`CHAT_INTERACTION_PUBSUB_TOPIC`, and `CHAT_APP_AUDIENCE` for the routes in use.
Grant it publish-only access to those topics. Grant the Attune runtime
subscriber-only access.

## Channel security

- Restrict `ATTUNE_SLACK_ALLOWED_USERS` and `ATTUNE_CHAT_ALLOWED_USERS`.
- Prefer an owner-only Slack DM. Google Chat resource names do not reveal space
  visibility; review membership before setting
  `ATTUNE_ACK_DESTINATION_VISIBILITY=1`.
- Use one approval channel. Multi-posting approval cards creates races and
  confusing duplicate decisions.
- Use a separate Google Chat app service account; never reuse the principal's
  Gmail/Calendar OAuth credential for proactive Chat messages.

## Operations

Use structured logs where supported, monitor loop heartbeats and retry state,
and alert on authentication failures rather than retrying them indefinitely.
Pin container image versions for production, terminate TLS at the platform for
the republisher, and test restoration of `ATTUNE_DATA_DIR` and Qdrant backups.
