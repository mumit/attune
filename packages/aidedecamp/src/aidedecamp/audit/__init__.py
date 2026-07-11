"""Structured audit / reason-for-action log (design doc 4.7).

Every proposed or taken action carries a short, structured reason, retrievable
later. Built in from day one (cheap early, expensive to retrofit), and a genuine
differentiator given the team's XAI focus and the market gap noted in 8.2.
"""

from .log import AuditEntry, AuditLog, JsonlAuditLog

__all__ = ["AuditEntry", "AuditLog", "JsonlAuditLog"]
