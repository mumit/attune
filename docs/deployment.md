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

For a non-container VM, install Attune into a dedicated virtual environment and
run it under the host service manager. A minimal systemd unit is:

```ini
[Unit]
Description=Attune workspace assistant
After=network-online.target

[Service]
Type=simple
User=attune
WorkingDirectory=/opt/attune
EnvironmentFile=/etc/attune/attune.env
ExecStart=/opt/attune/.venv/bin/attune run
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Keep the environment file readable only by the service account. Mount
`ATTUNE_DATA_DIR` and Qdrant storage on durable volumes; the source checkout
itself should be replaceable.

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
the republisher, and test restoration of backups.

Back up Attune while the process is stopped, or use filesystem snapshots that
are consistent across `ATTUNE_DATA_DIR` and Qdrant. The state directory contains
checkpoints, pending approvals, polling cursors, retry records, grants,
conversation windows, and the append-only audit log. Qdrant contains durable
memory vectors; back it up using its snapshot facility. Restore both to the same
point before restarting so workflow state and memory do not disagree.

Rotate credentials independently:

1. Create the replacement in the model gateway, MCP server, Google project, or
   channel platform.
2. Update the secret store/environment file without logging the value.
3. Restart Attune and run `attune doctor`.
4. Revoke the old credential only after Doctor and one read-only smoke test
   succeed.

Google OAuth authorized-user files and the Google Chat app service-account file
are separate credentials and should have separate rotation schedules.
