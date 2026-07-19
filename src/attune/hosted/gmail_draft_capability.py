"""Infrastructure-owned registration for the first hosted write capability.

``google.gmail.draft.create`` v1 is registered at product risk tier R2 --
the security architecture's own risk-tier table (security-architecture.md
section 8.2) lists a Gmail draft as its R2 example ("explicit approval by
default"), and this registration conforms to that normative table rather
than the other way around. Registering a definition here is necessary but
never sufficient to activate a write: no tenant holds an R2 autonomy grant,
no Google OAuth flow ever requests the ``gmail.compose`` scope this
capability requires, and the worker-side wiring that can reach this
registry at all sits behind ``ATTUNE_ENABLE_HOSTED_DRAFT_CAPABILITY``
(default off). See docs/capability-gateway.md for the full remaining-gates
list this slice does not close.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .capability_gateway import CapabilityDefinition, CapabilityRegistry, RiskTier

DRAFT_CAPABILITY = "google.gmail.draft.create"
DRAFT_CONTRACT_VERSION = 1
DRAFT_DOMAIN = "gmail_write"
DRAFT_PROVIDER = "google"
DRAFT_REQUIRED_SCOPES = ("https://www.googleapis.com/auth/gmail.compose",)

MAX_DRAFT_BODY_CHARS = 10_000
_THREAD_REF = re.compile(r"^[A-Za-z0-9_-]{1,180}$")


class GmailDraftCreateArguments:
    """Trusted schema for ``{thread_ref, body}`` -- the only two fields this
    capability's provider request needs. Exact keys only; both fields are
    bounded and validated before the gateway ever freezes them."""

    def reconstruct(self, value: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(value, Mapping) or set(value) != {"thread_ref", "body"}:
            raise ValueError("draft arguments must contain exactly thread_ref and body")
        thread_ref = value["thread_ref"]
        body = value["body"]
        if not isinstance(thread_ref, str) or not _THREAD_REF.fullmatch(thread_ref):
            raise ValueError("thread_ref must be a bounded Gmail thread identifier")
        if not isinstance(body, str) or not 1 <= len(body) <= MAX_DRAFT_BODY_CHARS:
            raise ValueError("body must contain between 1 and 10,000 characters")
        return {"thread_ref": thread_ref, "body": body}


def build_draft_capability_registry() -> CapabilityRegistry:
    """The one registered definition this stage activates behind its gate."""

    return CapabilityRegistry(
        (
            CapabilityDefinition(
                name=DRAFT_CAPABILITY,
                version=DRAFT_CONTRACT_VERSION,
                risk=RiskTier.R2,
                maximum_product_risk=RiskTier.R2,
                domain=DRAFT_DOMAIN,
                provider=DRAFT_PROVIDER,
                required_scopes=DRAFT_REQUIRED_SCOPES,
                arguments=GmailDraftCreateArguments(),
            ),
        )
    )
