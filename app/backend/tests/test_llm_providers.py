"""Tests for the provider factory and the OpenAI-compatible provider table. No network.

Run from app/backend:  python -m pytest tests
"""

import logging

import pytest
from langchain_openai import ChatOpenAI

from src.utils import llm_config, openai_compatible
from src.utils.bedrock_llm import BedrockConverseLLM
from src.utils.llm_providers import bedrock_model_name, build_chat_model
from src.utils.openai_compatible import chat_model_kwargs, eval_model_factory


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "VLLM_BASE_URL", "VLLM_API_KEY", "VLLM2_URL"):
        monkeypatch.delenv(name, raising=False)
    openai_compatible._warned.clear()


# --- provider name -> base URL and key env ------------------------------------


@pytest.mark.parametrize("provider", ["OpenRouter", "openrouter", "OPENROUTER"])
def test_openrouter_uses_default_base_url_and_key_env(provider):
    kwargs = chat_model_kwargs(provider, "mistralai/mistral-large-4-0", env={"OPENROUTER_API_KEY": "k"})
    assert kwargs["base_url"] == "https://openrouter.ai/api/v1"
    assert kwargs["api_key"] == "k"
    assert kwargs["model"] == "mistralai/mistral-large-4-0"


def test_openrouter_base_url_env_overrides_default():
    env = {"OPENROUTER_API_KEY": "k", "OPENROUTER_BASE_URL": "http://proxy:8080/v1"}
    assert chat_model_kwargs("OpenRouter", "m", env=env)["base_url"] == "http://proxy:8080/v1"


def test_openrouter_without_key_raises():
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        chat_model_kwargs("OpenRouter", "m", env={})


def test_vllm_needs_base_url():
    with pytest.raises(ValueError, match="VLLM_BASE_URL"):
        chat_model_kwargs("vLLM", "m", env={})


def test_vllm_key_is_optional():
    kwargs = chat_model_kwargs("vLLM", "Qwen/Qwen2.5-0.5B-Instruct", env={"VLLM_BASE_URL": "http://gpu:8000/v1"})
    assert kwargs["base_url"] == "http://gpu:8000/v1"
    assert kwargs["api_key"] == "not-needed"
    kwargs = chat_model_kwargs("vLLM", "m", env={"VLLM_BASE_URL": "http://gpu:8000/v1", "VLLM_API_KEY": "s"})
    assert kwargs["api_key"] == "s"


def test_custom_names_other_env_vars():
    env = {"VLLM_BASE_URL": "http://one/v1", "VLLM2_URL": "http://two/v1", "VLLM2_KEY": "two"}
    kwargs = chat_model_kwargs("vLLM", "m", custom={"base_url_env": "VLLM2_URL", "api_key_env": "VLLM2_KEY"}, env=env)
    assert kwargs["base_url"] == "http://two/v1"
    assert kwargs["api_key"] == "two"


def test_custom_base_url_env_has_no_default():
    with pytest.raises(ValueError, match="OTHER_URL"):
        chat_model_kwargs("OpenRouter", "m", custom={"base_url_env": "OTHER_URL"}, env={"OPENROUTER_API_KEY": "k"})


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="not an OpenAI-compatible provider"):
        chat_model_kwargs("Gemini", "m", env={})


# --- parameters ---------------------------------------------------------------


def test_reasoning_effort_and_allowlist_forwarded():
    params = {"reasoning_effort": "low", "max_tokens": 4000, "temperature": 0.2, "top_p": 0.9, "seed": 7, "stop": ["x"]}
    kwargs = chat_model_kwargs("OpenRouter", "m", params, env={"OPENROUTER_API_KEY": "k"})
    for key, value in params.items():
        assert kwargs[key] == value
    assert "extra_body" not in kwargs


def test_camel_case_aliases():
    kwargs = chat_model_kwargs("OpenRouter", "m", {"maxTokens": 100, "reasoningEffort": "high"},
                               env={"OPENROUTER_API_KEY": "k"})
    assert kwargs["max_tokens"] == 100
    assert kwargs["reasoning_effort"] == "high"


def test_nested_reasoning_goes_to_extra_body_for_openrouter():
    kwargs = chat_model_kwargs("OpenRouter", "m", {"reasoning": {"effort": "high"}}, env={"OPENROUTER_API_KEY": "k"})
    assert kwargs["extra_body"] == {"reasoning": {"effort": "high"}}


def test_nested_reasoning_dropped_with_warning_for_vllm(caplog):
    with caplog.at_level(logging.WARNING):
        kwargs = chat_model_kwargs("vLLM", "m", {"reasoning": {"effort": "high"}}, env={"VLLM_BASE_URL": "http://x/v1"})
    assert "extra_body" not in kwargs
    assert "'reasoning'" in caplog.text


def test_unknown_keys_logged_once(caplog):
    env = {"OPENROUTER_API_KEY": "k"}
    with caplog.at_level(logging.WARNING):
        kwargs = chat_model_kwargs("OpenRouter", "m", {"frequency_penalty": 1, "base_url": "http://evil"}, env=env)
        chat_model_kwargs("OpenRouter", "m", {"frequency_penalty": 1}, env=env)
    assert "frequency_penalty" not in kwargs
    assert kwargs["base_url"] == "https://openrouter.ai/api/v1"  # a parameter can't redirect the endpoint
    assert caplog.text.count("'frequency_penalty'") == 1
    assert caplog.text.count("'base_url'") == 1


# --- the factory ----------------------------------------------------------------


def test_build_chat_model_openrouter_sends_reasoning_effort(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    llm = build_chat_model("OpenRouter", "mistralai/mistral-large-4-0", {"reasoning_effort": "low", "max_tokens": 4000})
    assert isinstance(llm, ChatOpenAI)
    assert llm.openai_api_base == "https://openrouter.ai/api/v1"
    payload = llm._get_request_payload([("user", "hi")])
    assert payload["reasoning_effort"] == "low"
    assert payload["model"] == "mistralai/mistral-large-4-0"


def test_build_chat_model_nested_reasoning_in_request_body(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    llm = build_chat_model("OpenRouter", "m", {"reasoning": {"effort": "high"}})
    # The openai client merges extra_body into the JSON request body.
    assert llm._get_request_payload([("user", "hi")])["extra_body"] == {"reasoning": {"effort": "high"}}


def test_build_chat_model_bedrock():
    llm = build_chat_model("Bedrock:Anthropic", "us.anthropic.claude-sonnet-5", {"temperature": 0.1, "maxTokens": 99})
    assert isinstance(llm, BedrockConverseLLM)
    assert llm.model_id == "us.anthropic.claude-sonnet-5"
    assert llm.max_tokens == 99


def test_build_chat_model_unsupported_provider():
    with pytest.raises(ValueError, match="Unsupported LLM provider: Gemini"):
        build_chat_model("Gemini", "gemini-3")


def test_eval_model_factory_reads_handler_config(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    llm = eval_model_factory({
        "provider": {"name": "OpenRouter"},
        "model": {"name": "mistralai/mistral-large-4-0", "parameters": {"reasoning_effort": "high"}},
    })
    assert llm.model_name == "mistralai/mistral-large-4-0"
    assert llm.reasoning_effort == "high"


# --- Bedrock prefix, unchanged ----------------------------------------------------


@pytest.mark.parametrize("model,expected", [
    ("anthropic.claude-sonnet-5", "us.anthropic.claude-sonnet-5"),
    ("us.anthropic.claude-sonnet-5", "us.anthropic.claude-sonnet-5"),
    ("global.anthropic.claude-sonnet-5", "global.anthropic.claude-sonnet-5"),
])
def test_bedrock_model_name(model, expected):
    assert bedrock_model_name(model) == expected


class _FakeClient:
    def __init__(self, config):
        self.config = config

    def get_ai_config(self, key, context, default):
        return self.config, None, None


def _invoker_for(monkeypatch, config):
    monkeypatch.setattr(llm_config, "get_ld_client", lambda: _FakeClient(config))
    invoker, _ = llm_config.get_model_invoker("triage_agent", {})
    return invoker


def test_get_model_invoker_prefixes_bedrock_only(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    bedrock = _invoker_for(monkeypatch, {"provider": "Bedrock", "model": {"name": "anthropic.claude-sonnet-5"}})
    assert bedrock.model.model_id == "us.anthropic.claude-sonnet-5"
    assert bedrock.provider == "bedrock"

    openrouter = _invoker_for(monkeypatch, {
        "provider": "OpenRouter",
        "model": {"name": "mistralai/mistral-large-4-0", "parameters": {"reasoning_effort": "low", "max_tokens": 4000}},
    })
    assert openrouter.model.model_name == "mistralai/mistral-large-4-0"
    assert openrouter.model.reasoning_effort == "low"
    assert openrouter.model.max_tokens == 4000
    assert openrouter.provider == "openrouter"


def test_get_model_invoker_passes_custom(monkeypatch):
    monkeypatch.setenv("VLLM2_URL", "http://two/v1")
    invoker = _invoker_for(monkeypatch, {
        "provider": "vLLM",
        "model": {"name": "Qwen/Qwen2.5-0.5B-Instruct", "custom": {"base_url_env": "VLLM2_URL"}},
    })
    assert invoker.model.openai_api_base == "http://two/v1"
