# Roadmap

## Current foundation

- Attune package and CLI naming
- OpenAI-compatible SDK client with configurable task models
- Google OAuth and MCP workspace backends
- portable polling and advanced Google Pub/Sub ingestion
- independently configurable Slack and Google Chat routes
- editable, migration-aware `attune init`
- durable approvals, memory, audit, retries, and earned autonomy

## Near term

- publish and version the MCP tool contract; add live conformance fixtures
- exercise the Google Chat app callback and cards in a real test space
- add deployment examples for a generic VM and one container platform
- improve route diagnostics for missing channel credentials or destinations
- add backup/restore and credential-rotation runbooks

## Later

- richer calendar negotiation and follow-up workflows
- temporal/entity memory evaluation and optional Graphiti migration path
- additional channel adapters behind the same routing interface
- voice as a separate front door, without coupling it to a model provider
