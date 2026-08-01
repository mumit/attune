"""Attune exposed as an MCP server (build prompt 34, task 3).

A **separate process** from the credential-holding runtime — `CLAUDE.md`'s
"no public listener" rule and `docs/design.md` principle 5 are absolute for
that runtime, not for this one. This package holds no workspace, model, or
memory credential of its own: it depends only on the small read/propose
protocols in :mod:`attune.mcp_server.reader` and
:mod:`attune.mcp_server.proposals`, exactly the way the stateless
republisher and the hosted brokers depend only on their own narrow
boundaries (see ``deploy/republisher`` and ``attune.hosted.dispatch_broker``
for the precedent this package follows).

Read-only tools (:mod:`attune.mcp_server.reader`) ship separately from the
gated, approval-requiring ``attune.propose`` (:mod:`attune.mcp_server.gateway`,
:mod:`attune.mcp_server.tasks`) per the build prompt's own constraint: never
ship the write surface in the same change as the read surface. Both are
wired together, behind OAuth 2.1 Resource Indicator authorization
(:mod:`attune.mcp_server.auth`), in :mod:`attune.mcp_server.server`.
"""
