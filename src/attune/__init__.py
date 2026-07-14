"""Attune: a self-learning assistant over Gmail, Calendar, Chat, and Slack.

See docs/design.md for the full architecture, and the module docstrings below
for where each part of that design lives:

    config/       one-principal runtime configuration
    llm.py        OpenAI-compatible client and semantic model routing
    orchestrator/ LangGraph graphs (triage, draft-and-approve, schedule, brief)
    memory/       capture / consolidate / retrieve over Mem0 (-> Graphiti later)
    connectors/   swappable Workspace access (MCP or direct OAuth) + Slack
    ingestion/    portable polling and Google-specific event transports
    channels/     optional interaction/delivery surfaces (Slack, Google Chat)
    audit/        structured reason-for-action log (XAI, from day one)
"""

__version__ = "0.0.1"
