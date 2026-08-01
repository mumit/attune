# Attune Workspace MCP contract

Document version: **2.0** · Tool contract version: **1.1** · Client
transport/protocol version: **2.0**

This document tracks two independent things, and build prompt 34 is the
first change to touch only one of them:

- The **tool contract** — the six required Gmail/Calendar tools and their
  argument/result envelopes (below). Unchanged by this revision: still
  **1.1**, and every conformant 1.0 server remains conformant under it.
- The **transport/client protocol** — how `connectors/mcp_client.py` talks to
  whatever server implements the tool contract. This is what moved to **2.0**
  in build prompt 34, migrating to the MCP 2026-07-28 specification. See
  "Transport protocol" and the compatibility matrix below.

Tool contract version history: **1.0** — the original six required tools and
envelopes. **1.1** — adds three OPTIONAL calendar event result fields
(below); required tools, arguments, and all other envelope fields are
unchanged, so every conformant 1.0 server remains conformant under 1.1.

Document version history: **1.0/1.1** — tool contract only, no transport
section existed. **2.0** — adds this transport/client protocol section and
the compatibility matrix, and documents the 2026-07-28 stateless-core
migration in `connectors/mcp_client.py`. `CLAUDE.md` requires that changes to
required tools or envelopes version this document; a transport migration of
this size is the same kind of change even though the tool contract itself
didn't move, so it gets the same major-version bump and the same explicit
compatibility record.

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

## Transport protocol (client version 2.0, the 2026-07-28 migration)

The MCP maintainers describe the 2026-07-28 specification as the largest
change since authorization was added. Three changes to how Attune's client
talks to a server, all landed in `connectors/mcp_client.py`:

- **The core is stateless.** The `initialize`/`initialized` handshake and the
  `Mcp-Session-Id` header are eliminated. Attune's client (2.0) performs no
  handshake before a `tools/call` or `tools/list` request and sends no
  session-affinity header or field — a conformant server may sit behind
  plain round-robin with no sticky routing.
- **Client identity moves to per-request `_meta`.** Every request carries
  `params._meta["attune/client"] = {"name": "attune", "version": "2.0"}`
  instead of a one-time `initialize` payload's `clientInfo`. See
  `connectors/mcp_client._client_meta`.
- **Roots, Sampling, and Logging are deprecated** (functional through
  roughly May 2027 under the new 12-month lifecycle policy). Attune's client
  never used any of the three, so this migration removes no code on our
  side — noted here only because a future contribution must not add a new
  dependency on them.

A new extensions framework adds **MCP Apps** and **Tasks** (async
long-running operations with polling, mid-flight input, and a durable
handle). Attune does not consume Tasks as an MCP *client* here — that
extension is adopted on the *server* side, for `attune.propose`; see
`docs/decisions.md` and the MCP server's own docs.

### Client/server compatibility matrix

| Attune client version | Handshake sent | Session header/field sent | Works against a 2025-06-18 stateful server | Works against a 2026-07-28 stateless server |
| --- | --- | --- | --- | --- |
| 1.x (pre-migration) | Yes (`initialize`/`initialized`) | Yes (`Mcp-Session-Id`, SDK-managed) | Yes | Not attempted — never shipped against one |
| **2.0 (current)** | **No** | **No** | **Yes** — sends no handshake/session; a stateful server that tolerates being called without one still parses via the response's `structuredContent`/`content`, and any session bookkeeping it adds to the response (e.g. `_session_id`) is read and discarded, never required | **Yes** — this is the client's native target |

Server-side tool-contract version (1.1, above) is orthogonal to this table:
a server may be tool-contract 1.0 or 1.1 on *either* transport row. The
offline reference fixtures in `tests/test_mcp_client.py` exercise both the
"old envelope" (legacy session bookkeeping present in the response) and
"new envelope" (genuinely stateless response) shapes against the same
parsing code, which is what makes the "works against either" cell above a
tested claim rather than an assertion.

## Capability check and compatibility

`attune doctor` calls MCP `tools/list` on the configured logical Gmail and
Calendar servers and fails if a version-1 tool is absent. Adding optional tools
is backward compatible. Removing or renaming a required tool, changing its
argument meaning, or changing these result envelopes requires a new contract
version and a corresponding Attune adapter. Purely additive OPTIONAL result
fields take a minor version (1.x, e.g. the 1.1 event fields above); anything
a 1.0 server or adapter could observe as a behavior change takes a major one.

The transport/client protocol version above follows its own axis: a change
to handshake, session-affinity, or per-request metadata shape is a major
transport version regardless of whether the tool contract itself moved.

The offline connector suite is the reference conformance fixture for the
tool contract; `tests/test_mcp_client.py` is the reference fixture for the
transport/client protocol (the compatibility matrix above):

```bash
pytest tests/test_connectors.py tests/test_mcp_client.py -q
```
