"""Fuel iX configuration and model routing for Aide-de-camp.

This is the *only* place Fuel iX specifics live. The transport/auth mechanics
are handled by the generic ``bearer-openai`` package; this module supplies the
Fuel iX base URL, the verified model identifiers, and the task-shape -> model
routing described in the design doc (section 4.5).

Verified against TELUS Fuel iX (2026-07):
    base_url: https://api.fuelix.ai
    models:   claude-haiku-4-5, claude-sonnet-4-7, claude-sonnet-5,
              gpt-5.4, gpt-5.6-luna, gpt-5.6-terra

Routing philosophy (design doc 4.5): route by *task shape*, not a single default
model. Cheap/fast classification goes to a small model; drafting and multi-step
reasoning go to a strong model; nightly memory consolidation goes to the most
capable model because correctness there compounds over time. Keeping this map in
one module means it can be retuned centrally as gateway pricing/quality shifts,
rather than being hard-coded per graph.
"""

from __future__ import annotations

import os
from enum import Enum

from bearer_openai import AsyncBearerClient, BearerClient

FUELIX_BASE_URL = "https://api.fuelix.ai"

# Environment variable Aide-de-camp reads the Fuel iX bearer token from.
# In deployment this is populated from a secrets store (GCP Secret Manager on the
# TELUS side), never committed. Rotation = update the secret + restart.
FUELIX_TOKEN_ENV = "FUELIX_TOKEN"


class Model(str, Enum):
    """Verified Fuel iX model identifiers."""

    HAIKU_4_5 = "claude-haiku-4-5"
    SONNET_4_7 = "claude-sonnet-4-7"
    SONNET_5 = "claude-sonnet-5"
    GPT_5_4 = "gpt-5.4"
    GPT_5_6_LUNA = "gpt-5.6-luna"
    GPT_5_6_TERRA = "gpt-5.6-terra"


class EmbeddingModel(str, Enum):
    """Verified Fuel iX embedding model identifiers.

    Bundled with their output dimensionality because the vector store's
    ``embedding_model_dims`` MUST equal it, or the store rejects every insert.
    Use :meth:`dims` rather than hard-coding a number anywhere.
    """

    TEXT_3_LARGE = "text-embedding-3-large"
    TEXT_3_SMALL = "text-embedding-3-small"
    ADA_002 = "text-embedding-ada-002"

    @property
    def dims(self) -> int:
        return {
            "text-embedding-3-large": 3072,
            "text-embedding-3-small": 1536,
            "text-embedding-ada-002": 1536,
        }[self.value]


# Default embedder: 3-small is the quality/cost sweet spot. Upgrade to
# TEXT_3_LARGE (3072 dims) if retrieval quality proves limiting — that change
# requires recreating the vector collection, it is not a hot swap.
DEFAULT_EMBEDDING_MODEL = EmbeddingModel.TEXT_3_SMALL


class Task(str, Enum):
    """Task shapes the orchestrator routes on."""

    CLASSIFY = "classify"          # is this urgent? is this spam? which project?
    DRAFT = "draft"                # write a reply, a scheduling proposal
    REASON = "reason"              # multi-step planning, conflict resolution
    CONSOLIDATE = "consolidate"    # nightly memory consolidation
    CONVERSE = "converse"          # on-demand Q&A in a channel


# Task -> model routing. Uses currently-available Fuel iX models; retune here.
#   - Cheap/fast classification -> Haiku 4.5
#   - Drafting & conversational turns -> Sonnet 5
#   - Hard reasoning & nightly consolidation -> Sonnet 5 (most capable available)
DEFAULT_ROUTING: dict[Task, Model] = {
    Task.CLASSIFY: Model.HAIKU_4_5,
    Task.DRAFT: Model.SONNET_5,
    Task.REASON: Model.SONNET_5,
    Task.CONSOLIDATE: Model.SONNET_5,
    Task.CONVERSE: Model.SONNET_5,
}

MODEL_ENV: dict[Task, str] = {
    task: f"ADC_MODEL_{task.value.upper()}" for task in Task
}


def model_for(task: Task, routing: dict[Task, Model] | None = None) -> str:
    """Return the model id, allowing deployment-specific entitlement overrides."""
    table = routing or DEFAULT_ROUTING
    default = table[task].value
    return (
        default
        if routing is not None
        else os.environ.get(MODEL_ENV[task], default)
    )


def make_client(*, token: str | None = None, **openai_kwargs) -> BearerClient:
    """Construct a Fuel iX-backed synchronous client.

    Token resolution order (via bearer-openai): explicit ``token`` arg, then the
    ``FUELIX_TOKEN`` env var, then the ``BEARER_OPENAI_TOKEN`` fallback.
    """
    return BearerClient(
        base_url=FUELIX_BASE_URL,
        token=token,
        env_var=FUELIX_TOKEN_ENV,
        **openai_kwargs,
    )


def make_async_client(*, token: str | None = None, **openai_kwargs) -> AsyncBearerClient:
    """Construct a Fuel iX-backed asynchronous client."""
    return AsyncBearerClient(
        base_url=FUELIX_BASE_URL,
        token=token,
        env_var=FUELIX_TOKEN_ENV,
        **openai_kwargs,
    )
