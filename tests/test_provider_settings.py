from types import SimpleNamespace
from unittest.mock import Mock

import json
import pytest

from vivi_agent.actual_budget import ActualPriceUnknown
from vivi_agent.provider import OpenAIProvider, OpenRouterProvider, create_provider


def test_reasoning_judgment_settings_are_sent_and_recorded(tmp_path):
    response = SimpleNamespace(id="r", model="gpt-5.4-2026-03-05", status="completed",
                               output_text="{}", model_dump=lambda **_: {"usage": {}, "output": []})
    create = Mock(return_value=response)
    provider = OpenAIProvider(model=response.model, artifact_dir=tmp_path,
                              client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    provider.generate("review", "evidence", {"type": "object"})
    assert create.call_args.kwargs["reasoning"] == {"effort": "medium"}
    assert create.call_args.kwargs["max_output_tokens"] == 8000
    assert provider.last_receipt["reasoning_effort"] == "medium"
    provider.generate("design", "evidence", {"type": "object"})
    assert create.call_args.kwargs["reasoning"] == {"effort": "medium"}
    assert create.call_args.kwargs["max_output_tokens"] == 16000
    assert provider.last_receipt["reasoning_effort"] == "medium"


@pytest.mark.parametrize('effort', ['none', 'low', 'medium'])
def test_explicit_profile_is_sent_and_recorded_and_changes_cache(tmp_path, effort):
    response = SimpleNamespace(id="r", model="gpt-5.4-mini", status="completed",
                               output_text="{}", model_dump=lambda **_: {"usage": {}, "output": []})
    create = Mock(return_value=response)
    provider = OpenAIProvider(model=response.model, artifact_dir=tmp_path, reasoning_effort=effort,
                              max_output_tokens=3500,
                              client=SimpleNamespace(responses=SimpleNamespace(create=create)))
    provider.generate('design', 'evidence', {'type': 'object'})
    assert create.call_args.kwargs['reasoning'] == {'effort': effort}
    assert create.call_args.kwargs['max_output_tokens'] == 3500
    assert provider.last_receipt['generation_profile']['max_output_tokens'] == 3500
    assert provider.last_receipt['generation_profile']['reasoning_effort'] == effort
    other = OpenAIProvider(model=response.model, reasoning_effort=effort, max_output_tokens=3501, client=object())
    assert provider.cache_identity != other.cache_identity


def test_profile_environment_is_overridden_by_explicit_options_and_blank_is_default(monkeypatch):
    monkeypatch.setenv('VIVI_REASONING_EFFORT', 'low')
    monkeypatch.setenv('VIVI_MAX_OUTPUT_TOKENS', '4000')
    monkeypatch.setenv('VIVI_REASONING_EXCLUDE', 'false')
    env = create_provider('openrouter', client=object())
    assert env.generation_profile()['reasoning_effort'] == 'low'
    assert env.generation_profile()['max_output_tokens'] == 4000
    assert env.generation_profile()['reasoning_exclude'] is False
    explicit = create_provider('openrouter', client=object(), reasoning_effort='none',
                               max_output_tokens=3000, reasoning_exclude=True)
    assert explicit.generation_profile()['reasoning_effort'] == 'none'
    assert explicit.generation_profile()['max_output_tokens'] == 3000
    assert explicit.generation_profile()['reasoning_exclude'] is True
    assert env.cache_identity != explicit.cache_identity
    for key in ('VIVI_REASONING_EFFORT','VIVI_MAX_OUTPUT_TOKENS','VIVI_REASONING_EXCLUDE'):
        monkeypatch.setenv(key, '')
    default = create_provider('openrouter', client=object())
    assert default.generation_profile()['reasoning_effort'] == 'medium'
    assert default.generation_profile()['max_output_tokens'] == 16000
    assert default.generation_profile()['reasoning_exclude'] is True


@pytest.mark.parametrize('options', [{'reasoning_effort': 'cheap'}, {'max_output_tokens': 0},
                                   {'max_output_tokens': 128001}, {'max_output_tokens': 1.5},
                                   {'max_output_tokens': True}, {'reasoning_exclude': 'maybe'}])
def test_invalid_profiles_fail_before_provider_call(options):
    with pytest.raises(ValueError):
        OpenRouterProvider(client=object(), **options)


def test_estimator_includes_unicode_schema_and_instructions_without_network():
    provider = OpenRouterProvider(client=object(), max_output_tokens=3000)
    prompt = '한글 evidence 😀'
    schema = {'type': 'object', 'description': '설명' * 100}
    q = provider.estimated_max_cost(prompt, schema)
    assert q['input_token_upper_bound'] > len(prompt.encode()) + len(json.dumps(schema).encode()) + 4096
    assert q['max_output_tokens'] == 3000
    assert q['profile'] == provider.generation_profile()
    assert provider.last_receipt == {}
    with pytest.raises(ActualPriceUnknown):
        OpenRouterProvider(model='unknown/model', client=object()).estimated_max_cost(prompt, schema)
