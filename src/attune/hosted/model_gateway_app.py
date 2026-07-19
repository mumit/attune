"""Production composition root for the private hosted model gateway."""

from __future__ import annotations

import os

from .model_gateway import HostedModelGateway, make_openai_client
from .model_gateway_service import create_app


def create_production_app():
    gateway = HostedModelGateway(
        make_openai_client(
            base_url=os.environ["ATTUNE_LLM_BASE_URL"],
            api_key=os.environ["ATTUNE_LLM_API_KEY"],
        ),
        models={
            "classify": os.environ["ATTUNE_MODEL_CLASSIFY"],
            "converse": os.environ["ATTUNE_MODEL_CONVERSE"],
            "embed": os.environ["ATTUNE_MODEL_EMBED"],
        },
    )
    return create_app(
        gateway,
        expected_audience=os.environ["ATTUNE_EXPECTED_AUDIENCE"],
        expected_worker=os.environ["ATTUNE_WORKER_SERVICE_ACCOUNT"],
    )


app = create_production_app()
