import json
from pathlib import Path

import httpx
import pytest
from openai import OpenAI

from vivi_agent import cli
from vivi_agent.provider import (
    OpenAIProvider,
    OpenRouterProvider,
    ProviderError,
    create_provider,
    provider_api_key_name,
)


def completion(**overrides):
    body = {
        "id": "gen-test", "object": "chat.completion", "created": 1,
        "model": "openai/gpt-5.4", "provider": "OpenAI",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": '{"decision":"observe"}', "refusal": None,
        }}],
        "usage": {
            "prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150,
            "prompt_tokens_details": {"cached_tokens": 80},
            "completion_tokens_details": {"reasoning_tokens": 20},
            "cost": 0.0008, "cost_details": {"upstream_inference_cost": 0.0007},
        },
    }
    body.update(overrides)
    return body


def router(tmp_path, response=None, status=200):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json=completion() if response is None else response,
                              headers={"x-request-id": "req-test"})

    client = OpenAI(api_key="test-credential-sentinel", base_url="https://openrouter.ai/api/v1",
                    max_retries=0, timeout=180,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return OpenRouterProvider(client=client, artifact_dir=tmp_path), requests


def test_openrouter_sdk_contract_usage_and_cost(tmp_path):
    provider, requests = router(tmp_path)
    schema = {"type": "object", "properties": {"decision": {"type": "string"}},
              "required": ["decision"], "additionalProperties": False}
    assert provider.generate("design", "supplied evidence", schema) == {"decision": "observe"}
    assert len(requests) == 1
    assert requests[0].url.path == "/api/v1/chat/completions"
    body = json.loads(requests[0].content)
    assert body["response_format"] == {"type": "json_schema", "json_schema": {
        "strict": True, "name": "design", "schema": schema,
    }}
    assert body["reasoning"] == {"effort": "medium", "exclude": True}
    assert body["provider"] == {"require_parameters": True, "allow_fallbacks": False}
    assert body["max_completion_tokens"] == 16000
    assert body["stream"] is False
    assert body["messages"][1] == {"role": "user", "content": "supplied evidence"}
    usage = provider.last_receipt["usage"]
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (120, 30, 150)
    assert usage["input_tokens_details"]["cached_tokens"] == 80
    assert usage["output_tokens_details"]["reasoning_tokens"] == 20
    assert provider.last_receipt["raw_usage"]["cost_details"] == {"upstream_inference_cost": 0.0007}
    assert provider.last_receipt["actual_cost_usd"] == 0.0008
    assert provider.last_receipt["actual_cost_source"] == "OpenRouter response usage.cost"
    assert provider.last_receipt["actual_provider_calls"] == 1
    assert provider.last_receipt["upstream_provider"] == "OpenAI"
    trace = Path(provider.last_receipt["trace_path"]).read_text()
    assert "test-credential-sentinel" not in trace
    assert json.loads(trace)["provider_response"]["usage"]["cost"] == 0.0008


@pytest.mark.parametrize("finish_reason", ["length", "content_filter", "tool_calls", None])
def test_incomplete_response_is_rejected_without_losing_accounting(tmp_path, finish_reason):
    body = completion()
    body["choices"][0]["finish_reason"] = finish_reason
    provider, requests = router(tmp_path, body)
    with pytest.raises(ProviderError, match="did not complete"):
        provider.generate("review", "evidence", {"type": "object"})
    assert len(requests) == 1
    assert provider.last_receipt["status"] == "failed"
    assert provider.last_receipt["usage"]["output_tokens"] == 30
    assert provider.last_receipt["actual_cost_usd"] == 0.0008


@pytest.mark.parametrize("message", [
    {"role": "assistant", "content": "{}", "refusal": "Cannot comply"},
    {"role": "assistant", "content": None},
    {"role": "assistant", "content": "[]"},
    {"role": "assistant", "content": "not json"},
])
def test_refusal_and_non_object_output_are_rejected(tmp_path, message):
    body = completion()
    body["choices"][0]["message"] = message
    provider, requests = router(tmp_path, body)
    with pytest.raises(ProviderError):
        provider.generate("review", "evidence", {"type": "object"})
    assert len(requests) == 1
    assert provider.last_receipt["status"] == "failed"


@pytest.mark.parametrize("status", [200, 401, 429, 503])
def test_gateway_errors_do_not_leak_credentials_or_retry(tmp_path, status):
    provider, requests = router(tmp_path, {
        "error": {"message": "Invalid key test-credential-sentinel", "code": status},
    }, status)
    with pytest.raises(ProviderError) as error:
        provider.generate("diagnose", "supplied evidence", {"type": "object"})
    assert len(requests) == 1
    assert "test-credential-sentinel" not in str(error.value)
    assert "test-credential-sentinel" not in Path(provider.last_receipt["trace_path"]).read_text()


def test_missing_cost_is_unknown_and_trace_write_failure_does_not_repeat(tmp_path, monkeypatch):
    body = completion(usage={"prompt_tokens": 12, "completion_tokens": 3})
    provider, requests = router(tmp_path, body)

    def fail_trace(*_):
        raise OSError("read-only destination")

    monkeypatch.setattr(provider, "_save_trace", fail_trace)
    assert provider.generate("design", "evidence", {"type": "object"}) == {"decision": "observe"}
    assert provider.last_receipt["actual_cost_usd"] is None
    assert provider.last_receipt["trace_error_type"] == "OSError"
    assert provider.last_receipt["status"] == "completed"
    assert len(requests) == 1


def test_provider_selection_and_model_environment_are_separate(monkeypatch, tmp_path):
    monkeypatch.setenv("VIVI_PROVIDER", "openrouter")
    monkeypatch.setenv("VIVI_MODEL", "gpt-5.4-2026-03-05")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    provider = create_provider(client=object(), artifact_dir=tmp_path)
    assert isinstance(provider, OpenRouterProvider)
    assert provider.model == "openai/gpt-5.4"
    assert provider.cache_identity.startswith("openrouter:openai/gpt-5.4:")
    monkeypatch.setenv("OPENROUTER_MODEL", "router-configured-model")
    assert create_provider(client=object()).model == "router-configured-model"
    assert create_provider(model="explicit-model", client=object()).model == "explicit-model"
    direct = create_provider("openai", client=object())
    assert isinstance(direct, OpenAIProvider)
    assert direct.cache_identity.startswith("openai:gpt-5.4-2026-03-05:")
    assert provider_api_key_name() == "OPENROUTER_API_KEY"
    assert provider_api_key_name("openai") == "OPENAI_API_KEY"
    with pytest.raises(ValueError, match="openai or openrouter"):
        create_provider("unsupported", client=object())


def test_router_client_constructor_uses_router_credentials_and_no_retries(monkeypatch):
    from unittest.mock import Mock

    constructor = Mock()
    monkeypatch.setattr("openai.OpenAI", constructor)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-direct-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-test-key")
    OpenRouterProvider()
    constructor.assert_called_once_with(api_key="router-test-key",
                                        base_url="https://openrouter.ai/api/v1",
                                        max_retries=0, timeout=240)


@pytest.mark.parametrize('effort', ['low', 'none'])
def test_router_profile_controls_request_and_cache_identity(tmp_path, effort):
    base, requests = router(tmp_path)
    provider = OpenRouterProvider(client=base.client, artifact_dir=tmp_path,
                                  reasoning_effort=effort, max_output_tokens=3000,
                                  reasoning_exclude=False)
    provider.generate('design', 'evidence', {'type': 'object'})
    body = json.loads(requests[0].content)
    assert body['reasoning'] == {'effort': effort, 'exclude': False}
    assert body['max_completion_tokens'] == 3000
    assert body['provider']['allow_fallbacks'] is False
    assert provider.last_receipt['generation_profile'] == provider.generation_profile()
    assert provider.last_receipt['reasoning_effort'] == effort
    assert provider.cache_identity != base.cache_identity
    for changed in [{'reasoning_effort': 'medium'}, {'max_output_tokens': 3001}, {'reasoning_exclude': True}]:
        options = {'reasoning_effort': effort, 'max_output_tokens': 3000, 'reasoning_exclude': False} | changed
        assert provider.cache_identity != OpenRouterProvider(client=object(), **options).cache_identity


def test_error_envelope_numeric_charge_survives_without_secret_text(tmp_path):
    body = completion(error={'message': 'test-credential-sentinel'})
    provider, requests = router(tmp_path, response=body)
    with pytest.raises(ProviderError):
        provider.generate('design', 'evidence', {'type': 'object'})
    assert len(requests) == 1
    assert provider.last_receipt['actual_cost_usd'] == 0.0008
    assert 'test-credential-sentinel' not in Path(provider.last_receipt['trace_path']).read_text()


def test_cli_requires_the_selected_provider_key(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_dotenv", lambda *_, **__: None)
    monkeypatch.setenv("VIVI_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENAI_API_KEY", "direct-test-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("sys.argv", ["vivi-agent", "analyze", "lead"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "OPENROUTER_API_KEY is required" in captured.err
    assert "direct-test-key" not in captured.err
