import pytest

from attune.config import Settings, WorkspaceBackend
from attune.llm import LlmConfigurationError, Task, make_client, model_for


def test_models_are_selected_from_configuration():
    settings = Settings.from_env({
        "ATTUNE_MODEL_DEFAULT": "general-model",
        "ATTUNE_MODEL_CLASSIFY": "fast-model",
    })
    assert model_for(Task.CLASSIFY, settings) == "fast-model"
    assert model_for(Task.DRAFT, settings) == "general-model"


def test_missing_model_is_actionable():
    with pytest.raises(LlmConfigurationError, match="ATTUNE_MODEL_DRAFT"):
        model_for(Task.DRAFT, Settings.from_env({}))


def test_client_uses_configured_openai_compatible_gateway():
    settings = Settings.from_env({
        "ATTUNE_LLM_BASE_URL": "https://gateway.example/v1",
        "ATTUNE_LLM_API_KEY": "tok-123",
    })
    client = make_client(settings=settings)
    assert client.api_key == "tok-123"
    assert str(client.base_url).rstrip("/") == "https://gateway.example/v1"


def test_missing_api_key_is_actionable():
    with pytest.raises(LlmConfigurationError, match="ATTUNE_LLM_API_KEY"):
        make_client(settings=Settings.from_env({}))


def test_settings_default_to_google_oauth_backend():
    assert Settings.from_env({}).workspace_backend == WorkspaceBackend.GOOGLE_OAUTH
