"""Tests for the Azure AI Foundry branch of the provider switch. No network.

Run from app/backend:  python -m pytest tests
"""

import pytest
from langchain_openai import ChatOpenAI

from src.utils.llm_config import AZURE_PROVIDERS, _create_llm_for_provider, azure_foundry_base_url

RESOURCE = "https://example-resource.services.ai.azure.com"


@pytest.fixture
def azure_env(monkeypatch):
    monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", RESOURCE)
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "test-key")


@pytest.mark.parametrize("provider", sorted(AZURE_PROVIDERS))
def test_azure_provider_builds_chat_openai_on_v1_route(azure_env, provider):
    llm = _create_llm_for_provider(provider, "grok-4.6", 0.1, 500)
    assert isinstance(llm, ChatOpenAI)
    assert llm.model_name == "grok-4.6"
    assert llm.openai_api_base == f"{RESOURCE}/openai/v1/"
    assert llm.openai_api_key.get_secret_value() == "test-key"
    assert llm.temperature == 0.1
    assert llm.max_tokens == 500


def test_provider_string_from_model_config(azure_env):
    # The model config's provider is "Azure AI Foundry"; get_model_invoker lowercases it before the switch.
    assert isinstance(_create_llm_for_provider("Azure AI Foundry", "grok-4.6", None, 100), ChatOpenAI)


@pytest.mark.parametrize(
    "endpoint",
    [
        RESOURCE,
        f"{RESOURCE}/",
        f"{RESOURCE}/openai/v1/chat/completions",
        f"{RESOURCE}/openai/v1/",
        f"  {RESOURCE}/models?api-version=2024-05-01-preview ",
    ],
)
def test_endpoint_path_is_stripped(endpoint):
    assert azure_foundry_base_url(endpoint) == f"{RESOURCE}/openai/v1/"


def test_endpoint_without_scheme_raises():
    with pytest.raises(ValueError, match="AZURE_FOUNDRY_ENDPOINT is not a URL"):
        azure_foundry_base_url("example-resource.services.ai.azure.com")


def test_missing_endpoint_raises(monkeypatch):
    monkeypatch.delenv("AZURE_FOUNDRY_ENDPOINT", raising=False)
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "test-key")
    with pytest.raises(ValueError, match="AZURE_FOUNDRY_ENDPOINT not set"):
        _create_llm_for_provider("azure ai foundry", "grok-4.6", None, 100)


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", RESOURCE)
    monkeypatch.delenv("AZURE_FOUNDRY_API_KEY", raising=False)
    with pytest.raises(ValueError, match="AZURE_FOUNDRY_API_KEY not set"):
        _create_llm_for_provider("azure ai foundry", "grok-4.6", None, 100)
