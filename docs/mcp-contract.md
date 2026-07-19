# Attune Workspace MCP contract

Contract version: **1.1**

Version history: **1.0** — the original six required tools and envelopes.
**1.1** — adds three OPTIONAL calendar event result fields (below); required
tools, arguments, and all other envelope fields are unchanged, so every
conformant 1.0 server remains conformant under 1.1.

Attune can use any MCP package or remote server that exposes this tool contract.
The server owns provider credentials, consent, provider-specific API calls, and
its own policy/audit controls. Attune connects over MCP Streamable HTTP and may
authenticate to the server with `ATTUNE_MCP_TOKEN`.

Configure one shared endpoint with `ATTUNE_MCP_URL`, or separate endpoints with
`ATTUNE_MCP_GMAIL_URL` and `ATTUNE_MCP_CALENDAR_URL`. Logical server names below
select the endpoint; they are not assumptions about the server vendor.

## Gmail tools

### `search_threads`

Arguments:

```json
{"query": "is:unread", "max_results": 20}
```

Result:

```json
{
  "threads": [{
    "thread_id": "string",
    "subject": "string",
    "snippet": "string",
    "from": "person@example.com",
    "body": "string",
    "labels": ["string"],
    "last_from": "person@example.com",
    "last_message_at": "2026-07-13T12:00:00+00:00",
    "reply_to": "person@example.com"
  }]
}
```

Only `thread_id` is structurally required. Missing descriptive fields default
to empty values. Returned content is always treated as fetched/untrusted.

### `get_thread`

Arguments: `{"thread_id": "string"}`. Result: one thread object using the
shape above, without the outer `threads` array.

### `create_draft`

Arguments:

```json
{
  "to": "person@example.com",
  "subject": "string",
  "body": "string",
  "thread_id": "optional string"
}
```

Result: `{"draft_id": "string"}`. Contract v1 deliberately has no send tool.

### `modify_labels`

Arguments:

```json
{"thread_id": "string", "add_labels": ["Followup"]}
```

The result may be an empty object.

## Calendar tools

### `list_events`

Arguments use RFC 3339 timestamps:

```json
{
  "time_min": "2026-07-13T00:00:00+00:00",
  "time_max": "2026-07-20T00:00:00+00:00"
}
```

Result:

```json
{
  "events": [{
    "event_id": "string",
    "summary": "string",
    "start": "2026-07-13T09:00:00+00:00",
    "end": "2026-07-13T09:30:00+00:00",
    "attendees": ["person@example.com"]
  }]
}
```

### `get_event`

Arguments: `{"event_id": "string"}`. Result: one event object using the shape
above, without the outer `events` array.

### Optional event fields (contract 1.1)

An event object may also include `organizer` (string email), `organizer_is_self`
(bool — true when the PRINCIPAL organizes this event), and `response_status`
(string — the principal's own attendee responseStatus, e.g. `"needsAction"`).
All three are OPTIONAL: a server that omits them gets Attune's safe defaults
(`""`/`False`/`""`), under which neither the decline-invite nor the
reschedule proposal path can ever fire. Contract 1.1 still has no
decline-invite or reschedule-event tool regardless of what these fields
report — `supports_calendar_writes()` stays `False` for the MCP connector
(see `docs/decisions.md`).

## Capability check and compatibility

`attune doctor` calls MCP `tools/list` on the configured logical Gmail and
Calendar servers and fails if a version-1 tool is absent. Adding optional tools
is backward compatible. Removing or renaming a required tool, changing its
argument meaning, or changing these result envelopes requires a new contract
version and a corresponding Attune adapter. Purely additive OPTIONAL result
fields take a minor version (1.x, e.g. the 1.1 event fields above); anything
a 1.0 server or adapter could observe as a behavior change takes a major one.

The offline connector suite is the reference conformance fixture:

```bash
pytest tests/test_connectors.py -q
```
