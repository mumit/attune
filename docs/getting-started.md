# Getting started

*This page has moved. See [`modes.md`](modes.md) for the full menu of ways to
run Attune.*

The self-hosted, single-principal setup guide — the shortest path to one
working Attune instance for one person — now lives at
[`install/self-hosted.md`](install/self-hosted.md). It absorbs everything this
page used to cover: installing the checkout, guided local setup
(`attune init --target local`), the Google OAuth or MCP workspace backend
choice, optional Slack, validation, and common failures.

Two pieces of that runbook are their own canonical documents, since the same
ceremonies are shared with other parts of this repository:

- [`install/google-workspace-oauth.md`](install/google-workspace-oauth.md) —
  the Google Cloud Console OAuth ceremony (also used by the Google Pub/Sub
  push variant in [`deployment.md`](deployment.md)).
- [`install/slack-app.md`](install/slack-app.md) — Slack app creation (also
  covers the hosted platform's Slack app, in its own clearly separated
  section).

If you want an always-on server deployment, the Google Cloud Pub/Sub push
transport variant, or the hosted multi-tenant operator runbook, see
[`modes.md`](modes.md) for the complete menu.
