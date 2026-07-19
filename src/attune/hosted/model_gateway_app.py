"""Production composition root for the private hosted model gateway."""

from __future__ import annotations

import os

from .model_gateway import HostedModelGateway, make_openai_client
from .model_gateway_service import create_app


def create_production_app():
    standard_models = {
        "classify": os.environ["ATTUNE_MODEL_CLASSIFY"],
        "converse": os.environ["ATTUNE_MODEL_CONVERSE"],
        "embed": os.environ["ATTUNE_MODEL_EMBED"],
    }
    profiles_enabled_value = os.environ.get(
        "ATTUNE_ENABLE_TENANT_MODEL_PROFILES", "false"
    )
    if profiles_enabled_value not in {"true", "false"}:
        raise ValueError("ATTUNE_ENABLE_TENANT_MODEL_PROFILES must be true or false")
    profiles_enabled = profiles_enabled_value == "true"
    # Gate off (the default): ``profiles=None`` below is the pinned
    # byte-identical path -- HostedModelGateway._resolve_model always
    # returns the fixed ``standard_models`` route for every task, exactly
    # as it did before this feature existed. Gate on: the operator's own
    # env-shaped premium routes join the standard map into one profile
    # mapping, matching how the fixed six ATTUNE_MODEL_* routes were
    # already configured (never a tenant- or worker-supplied endpoint).
    profiles = None
    if profiles_enabled:
        profiles = {
            "standard": standard_models,
            "premium": {
                "classify": os.environ["ATTUNE_MODEL_PREMIUM_CLASSIFY"],
                "converse": os.environ["ATTUNE_MODEL_PREMIUM_CONVERSE"],
                "embed": os.environ["ATTUNE_MODEL_PREMIUM_EMBED"],
            },
        }
    gateway = HostedModelGateway(
        make_openai_client(
            base_url=os.environ["ATTUNE_LLM_BASE_URL"],
            api_key=os.environ["ATTUNE_LLM_API_KEY"],
        ),
        models=standard_models,
        profiles=profiles,
    )
    return create_app(
        gateway,
        expected_audience=os.environ["ATTUNE_EXPECTED_AUDIENCE"],
        expected_worker=os.environ["ATTUNE_WORKER_SERVICE_ACCOUNT"],
        profiles_enabled=profiles_enabled,
    )


app = create_production_app()
