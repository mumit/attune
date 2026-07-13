from aidedecamp.fuelix import (
    FUELIX_BASE_URL,
    Model,
    Task,
    make_client,
    model_for,
)
from aidedecamp.config import ConnectorMode, Deployment, Settings


def test_base_url():
    assert FUELIX_BASE_URL == "https://api.fuelix.ai"


def test_routing_matches_design():
    assert model_for(Task.CLASSIFY) == Model.HAIKU_4_5.value
    assert model_for(Task.DRAFT) == Model.SONNET_5.value
    assert model_for(Task.REASON) == Model.SONNET_5.value
    assert model_for(Task.CONSOLIDATE) == Model.SONNET_5.value
    assert model_for(Task.CONVERSE) == Model.SONNET_5.value


def test_model_routing_can_follow_deployment_entitlements(monkeypatch):
    monkeypatch.setenv("ADC_MODEL_DRAFT", "gpt-5.6-luna")
    assert model_for(Task.DRAFT) == "gpt-5.6-luna"


def test_client_resolves_token_from_fuelix_env(monkeypatch):
    monkeypatch.setenv("FUELIX_TOKEN", "tok-123")
    c = make_client()
    assert c.api_key == "tok-123"
    assert str(c.base_url).rstrip("/") == FUELIX_BASE_URL


def test_settings_from_env_defaults():
    s = Settings.from_env(env={})
    assert s.deployment == Deployment.PERSONAL
    assert s.connector_mode == ConnectorMode.DIRECT_OAUTH


def test_settings_telus_direct_oauth():
    s = Settings.from_env(
        env={"ADC_DEPLOYMENT": "telus", "ADC_CONNECTOR_MODE": "direct_oauth"}
    )
    assert s.deployment == Deployment.TELUS
    assert s.connector_mode == ConnectorMode.DIRECT_OAUTH
