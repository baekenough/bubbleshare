"""OTLP 계약과 계측 실패 시 본 호출 유지 정책을 검증한다. 유료 호출은 없다."""
import base64
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vivi_agent import observability, provider as provider_module
from vivi_agent.observability import emit_generation, generation_payload, usage_details
from vivi_agent.provider import OpenAIProvider, OpenRouterProvider, ProviderError


@pytest.fixture(autouse=True)
def no_inherited_langfuse(monkeypatch):
    for name in ["LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]:
        monkeypatch.delenv(name, raising=False)


def receipt(**updates):
    value = {
        "invocation_id": "d" * 32, "provider": "openrouter", "stage": "design",
        "requested_model": "openai/gpt-5.4", "model": "openai/gpt-5.4",
        "started_at": "2026-10-15T01:00:00.000000+00:00",
        "finished_at": "2026-10-15T01:00:01.250000+00:00", "latency_ms": 1250.0,
        "status": "completed", "actual_provider_calls": 1, "prompt_hash": "a" * 64,
        "actual_cost_usd": 0.12345,
        "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
                  "input_tokens_details": {"cached_tokens": 80},
                  "output_tokens_details": {"reasoning_tokens": 20}},
    }
    return value | updates


def span(payload):
    resources = payload["resourceSpans"]
    assert len(resources) == 1
    scopes = resources[0]["scopeSpans"]
    assert len(scopes) == 1
    assert len(scopes[0]["spans"]) == 1
    return scopes[0]["spans"][0]


def attributes(payload):
    return {item["key"]: next(iter(item["value"].values())) for item in span(payload)["attributes"]}


def configure(monkeypatch, url="http://localhost:3000"):
    monkeypatch.setenv("LANGFUSE_BASE_URL", url)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-secret-test")


def test_one_root_generation_exact_cost_exclusive_usage_and_safe_metadata():
    value = receipt(prompt="prompt-private", result={"secret": "output-private"},
                    error="key-private", headers={"Authorization": "key-private"})
    payload = generation_payload(value)
    item = span(payload)
    attrs = attributes(payload)
    assert attrs["langfuse.observation.type"] == "generation"
    assert attrs["langfuse.observation.model.name"] == "openai/gpt-5.4"
    assert json.loads(attrs["langfuse.observation.usage_details"]) == {
        "input": 40, "input_cached_tokens": 80, "output": 10,
        "output_reasoning_tokens": 20, "total": 150,
    }
    assert json.loads(attrs["langfuse.observation.cost_details"]) == {"total": 0.12345}
    assert attrs["langfuse.observation.metadata.assignment_model_cost_usd"] == 0.018
    assert attrs["langfuse.observation.metadata.actual_cost_status"] == "known"
    assert attrs["langfuse.observation.metadata.latency_ms"] == 1250.0
    assert item["traceId"] == "d" * 32 and len(item["spanId"]) == 16
    assert int(item["endTimeUnixNano"]) - int(item["startTimeUnixNano"]) == 1_250_000_000
    assert item["status"]["code"] == 1
    assert generation_payload(value) == payload
    assert all(text not in json.dumps(payload) for text in ["prompt-private", "output-private", "key-private"])


@pytest.mark.parametrize("cost", [None, True, False, -1, float("nan"), float("inf"), "0.5"])
def test_unknown_or_invalid_cost_is_not_zero_or_assignment_price(cost):
    attrs = attributes(generation_payload(receipt(actual_cost_usd=cost)))
    assert "langfuse.observation.cost_details" not in attrs
    assert attrs["langfuse.observation.metadata.actual_cost_status"] == "unknown"
    assert attrs["langfuse.observation.metadata.actual_cost_usd"] == "unknown"


def test_genuinely_zero_provider_cost_is_known_zero():
    attrs = attributes(generation_payload(receipt(actual_cost_usd=0)))
    assert json.loads(attrs["langfuse.observation.cost_details"]) == {"total": 0}
    assert attrs["langfuse.observation.metadata.actual_cost_status"] == "known"


def test_missing_usage_is_not_invented_and_invalid_subcounts_do_not_double_count():
    assert usage_details(receipt(usage=None)) == ({}, {})
    details, inclusive = usage_details(receipt(usage={
        "input_tokens": 10, "output_tokens": 5,
        "input_tokens_details": {"cached_tokens": 99},
        "output_tokens_details": {"reasoning_tokens": -1},
    }))
    assert details == {"input": 10, "output": 5, "total": 15}
    assert inclusive["cached_tokens"] == 99


def test_missing_configuration_never_uses_a_cloud_default(monkeypatch):
    def fail(*args):
        raise AssertionError("No exporter must run")
    monkeypatch.setattr(observability, "_post", fail)
    assert emit_generation(receipt()) == {"enabled": False, "status": "disabled"}
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "not-exported")
    assert emit_generation(receipt())["status"] == "configuration_incomplete"


@pytest.mark.parametrize("url", ["https://cloud.langfuse.com", "https://example.com", "http://8.8.8.8",
                                 "http://user:secret@localhost:3000", "http://localhost:3000?token=secret",
                                 "http://localhost:3000/#secret", "file:///tmp/trace", "http://169.254.169.254"])
def test_non_local_or_credential_bearing_urls_cannot_export(monkeypatch, url):
    configure(monkeypatch, url)
    calls = []
    monkeypatch.setattr(observability, "_post", lambda *args: calls.append(args))
    result = emit_generation(receipt())
    assert result["status"] == "failed" and result["error_type"] == "ValueError"
    assert calls == [] and "secret" not in json.dumps(result)


@pytest.mark.parametrize("url", ["http://localhost:3000", "http://127.0.0.1:3000", "http://[::1]:3000",
                                 "http://langfuse-web:3000", "http://10.1.2.3:3000"])
def test_local_configuration_exports_once_and_reports_ids(monkeypatch, url):
    configure(monkeypatch, url)
    calls = []
    def accept(*args):
        calls.append(args)
        return {"enabled": True, "status": "accepted"}
    monkeypatch.setattr(observability, "_post", accept)
    result = emit_generation(receipt())
    assert result["status"] == "accepted" and result["trace_id"] == "d" * 32
    assert len(result["observation_id"]) == 16 and len(calls) == 1


def test_export_exception_is_minimal_status_without_error_text(monkeypatch):
    configure(monkeypatch)
    def fail(*args):
        raise RuntimeError("sk-lf-secret-test Authorization: do not persist")
    monkeypatch.setattr(observability, "_post", fail)
    assert emit_generation(receipt()) == {"enabled": True, "status": "failed", "error_type": "RuntimeError"}


@pytest.mark.parametrize("http_status,body,expected", [
    (200, b"{}", "accepted"), (200, b"", "accepted"),
    (200, b'{"partialSuccess":{"rejectedSpans":"1","errorMessage":"secret"}}', "failed"),
    (302, b"secret redirect", "failed"), (401, b"secret auth failure", "failed"),
])
def test_http_auth_is_header_only_and_redirect_or_partial_ingestion_does_not_retry(monkeypatch, http_status, body, expected):
    configure(monkeypatch)
    connections = []
    class Connection:
        def __init__(self, *args, **kwargs):
            self.requests, self.closed = [], False
            connections.append(self)
            assert kwargs["timeout"] == 2
        def request(self, *args, **kwargs): self.requests.append((args, kwargs))
        def getresponse(self): return SimpleNamespace(status=http_status, read=lambda maximum: body)
        def close(self): self.closed = True
    monkeypatch.setattr(observability.http.client, "HTTPConnection", Connection)
    result = emit_generation(receipt())
    assert result["status"] == expected and len(connections) == 1
    connection = connections[0]
    assert len(connection.requests) == 1 and connection.closed
    args, kwargs = connection.requests[0]
    assert args == ("POST", "/api/public/otel/v1/traces")
    assert kwargs["headers"]["Authorization"] == "Basic " + base64.b64encode(b"pk-lf-test:sk-lf-secret-test").decode()
    assert b"sk-lf-secret-test" not in kwargs["body"] and b"pk-lf-test" not in kwargs["body"]
    assert "secret" not in json.dumps(result)


def fake_provider(kind, tmp_path, fail=False):
    calls = []
    usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
             "input_tokens_details": {"cached_tokens": 50}, "output_tokens_details": {"reasoning_tokens": 5}}
    if kind == "openai":
        response = SimpleNamespace(id="test-id", model="gpt-5.4", status="completed", output_text='{"ok":true}',
                                   model_dump=lambda **kwargs: {"output": [], "usage": usage})
    else:
        response = SimpleNamespace(model_dump=lambda **kwargs: {
            "id": "test-id", "model": "openai/gpt-5.4", "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "cost": 0.05},
        })
    def create(**kwargs):
        calls.append(kwargs)
        if fail:
            raise RuntimeError("private-provider-key")
        return response
    client = SimpleNamespace(responses=SimpleNamespace(create=create),
                             chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    adapter = OpenAIProvider if kind == "openai" else OpenRouterProvider
    return adapter(client=client, artifact_dir=tmp_path), calls


@pytest.mark.parametrize("kind", ["openai", "openrouter"])
@pytest.mark.parametrize("fail", [False, True])
def test_provider_records_exactly_one_generation_per_attempt_even_on_failure(monkeypatch, tmp_path, kind, fail):
    emissions = []
    def observe(value):
        emissions.append(deepcopy(value))
        return {"enabled": True, "status": "accepted"}
    monkeypatch.setattr(provider_module, "emit_generation", observe)
    adapter, calls = fake_provider(kind, tmp_path, fail)
    if fail:
        with pytest.raises(ProviderError):
            adapter.generate("design", "private-input", {"type": "object"})
    else:
        assert adapter.generate("design", "private-input", {"type": "object"}) == {"ok": True}
    assert len(calls) == len(emissions) == 1
    emitted = emissions[0]
    assert emitted["status"] == ("failed" if fail else "completed")
    assert emitted["actual_provider_calls"] == 1 and emitted["latency_ms"] >= 0
    assert emitted["assignment_model_cost_usd"] == 0.018
    assert emitted["actual_cost_usd"] == (0.05 if kind == "openrouter" and not fail else None)
    assert adapter.last_receipt["observability"]["status"] == "accepted"
    trace = json.loads(Path(adapter.last_receipt["trace_path"]).read_text())
    assert trace["receipt"]["observability"]["status"] == "accepted"
    assert "private-provider-key" not in json.dumps(emitted)


@pytest.mark.parametrize("kind", ["openai", "openrouter"])
def test_exporter_regression_cannot_break_or_retry_successful_provider_call(monkeypatch, tmp_path, kind):
    def fail(*args):
        raise RuntimeError("secret-observer-error")
    monkeypatch.setattr(provider_module, "emit_generation", fail)
    adapter, calls = fake_provider(kind, tmp_path)
    assert adapter.generate("design", "private-input", {"type": "object"}) == {"ok": True}
    assert len(calls) == 1
    assert adapter.last_receipt["observability"] == {"enabled": True, "status": "failed", "error_type": "RuntimeError"}


@pytest.mark.parametrize("kind", ["openai", "openrouter"])
def test_scope_metadata_is_allowlisted_and_names_trace_by_topic(monkeypatch, tmp_path, kind):
    emissions = []
    def observe(value):
        emissions.append(generation_payload(value))
        return {"enabled": True, "status": "accepted"}
    monkeypatch.setattr(provider_module, "emit_generation", observe)
    adapter, calls = fake_provider(kind, tmp_path)
    expected = {"topic_id": "security", "snapshot_id": "SNAP-2", "question_id": "Q026",
                "policy_version": "product-one-call.v2", "dataset_version": "c" * 64, "run_key_hash": "d" * 64}
    adapter.observation_context = expected | {"customer_id": "private-customer", "context": "private-context"}
    adapter.generate("design", "input", {"type": "object"})
    assert len(calls) == 1 and len(emissions) == 1
    assert adapter.last_receipt["observation_context"] == expected
    attrs = attributes(emissions[0])
    assert attrs["langfuse.trace.name"] == "vivi.security.design"
    assert span(emissions[0])["name"] == "vivi.security.design"
    for key, value in expected.items():
        assert attrs[f"langfuse.observation.metadata.{key}"] == value
    assert "private-customer" not in json.dumps(emissions[0])
    assert "private-context" not in json.dumps(adapter.last_receipt)


def test_scope_metadata_rejects_nonidentifiers_and_nonhash_versions():
    context = {"topic_id": "topic with free text", "snapshot_id": "SNAP-2\nsecret",
               "question_id": {}, "policy_version": "x" * 201,
               "dataset_version": "not-a-hash", "run_key_hash": "b" * 63,
               "raw_context": "private"}
    payload = generation_payload(receipt(observation_context=context))
    attrs = attributes(payload)
    assert attrs["langfuse.trace.name"] == "vivi.design"
    assert not any(key.startswith("langfuse.observation.metadata." + item) for key in attrs for item in context)
    assert "private" not in json.dumps(payload)
