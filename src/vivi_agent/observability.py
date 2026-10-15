"""공급자 호출마다 최소 메타데이터만 Langfuse에 보내며 관측 실패는 생성에 전파하지 않는다.

Langfuse v3.22부터 제공되는 OTLP HTTP/JSON 엔드포인트를 사용한다.
https://langfuse.com/integrations/native/opentelemetry
토큰 사용량은 비용 계산 시 중복되지 않도록 나눈다.
https://langfuse.com/docs/observability/features/token-and-cost-tracking

클라우드 기본값·본문 수집·재시도·추가 모델 호출은 없다. 실제 공급자 비용과 과제 가정
비용을 구분한다. 실제 비용이 없을 때 Langfuse가 추정한 가격은 영수증으로 취급하지 않는다.
"""
from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
from urllib.parse import urlsplit


ASSIGNMENT_MODEL_COST_USD = 0.018
EXPORT_TIMEOUT_SECONDS = 2
_LOCAL_HOSTS = {"localhost", "langfuse", "langfuse-web", "host.docker.internal"}
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(network) for network in
                          ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))


def actual_cost(value):
    """확인된 0 이상의 달러 비용을 반환한다. 실제 0도 허용하며 알 수 없으면 None이다."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return value
    return None


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./:@+-]{1,200}", value) else None


def observation_context(value):
    """명시적으로 허용한 비개인 범위 식별자와 SHA-256 해시만 남긴다."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("topic_id", "snapshot_id", "question_id", "policy_version"):
        item = _identifier(value.get(key))
        if item is not None:
            result[key] = item
    for key in ("dataset_version", "run_key_hash"):
        item = value.get(key)
        if isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item):
            result[key] = item
    return result


def usage_details(receipt):
    """캐시·추론 토큰을 중복 계산하지 않도록 공급자의 포함 관계를 풀어낸다."""
    usage = receipt.get("usage") or {}
    details, inclusive = {}, {}
    for name, provider_key, detail_key, subkey, bucket in [
        ("input", "input_tokens", "input_tokens_details", "cached_tokens", "input_cached_tokens"),
        ("output", "output_tokens", "output_tokens_details", "reasoning_tokens", "output_reasoning_tokens"),
    ]:
        count = _count(usage.get(provider_key))
        subcount = _count((usage.get(detail_key) or {}).get(subkey))
        if count is not None:
            inclusive[provider_key] = count
            if subcount is not None and subcount <= count:
                details[name], details[bucket] = count - subcount, subcount
            else:
                details[name] = count
        if subcount is not None:
            inclusive[subkey] = subcount
    total = _count(usage.get("total_tokens"))
    if total is not None:
        details["total"] = total
    elif "input_tokens" in inclusive and "output_tokens" in inclusive:
        details["total"] = inclusive["input_tokens"] + inclusive["output_tokens"]
    return details, inclusive


def _attribute(key, value):
    if type(value) is bool:
        encoded = {"boolValue": value}
    elif type(value) is int:
        encoded = {"intValue": str(value)}
    elif isinstance(value, float):
        encoded = {"doubleValue": value}
    else:
        encoded = {"stringValue": str(value)}
    return {"key": key, "value": encoded}


def _timestamp_ns(value):
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ValueError("Telemetry timestamps must include their timezone")
    return str(int(timestamp.timestamp()) * 1_000_000_000 + timestamp.microsecond * 1000)


def generation_payload(receipt):
    """허용된 메타데이터로 OTLP 최상위 생성 관측 한 개를 만든다."""
    invocation_id = receipt["invocation_id"]
    trace_id = (invocation_id if re.fullmatch(r"[0-9a-f]{32}", invocation_id)
                else hashlib.sha256(invocation_id.encode()).hexdigest()[:32])
    span_id = hashlib.sha256((invocation_id + ":generation").encode()).hexdigest()[:16]
    stage = _identifier(receipt.get("stage")) or "generation"
    context = observation_context(receipt.get("observation_context"))
    trace_name = f"vivi.{context['topic_id']}.{stage}" if context.get("topic_id") else f"vivi.{stage}"
    model = _identifier(receipt.get("model")) or _identifier(receipt.get("requested_model"))
    cost = actual_cost(receipt.get("actual_cost_usd"))
    usage, inclusive = usage_details(receipt)
    status = "completed" if receipt.get("status") == "completed" else "failed"
    values = {
        "langfuse.observation.type": "generation",
        "langfuse.observation.level": "DEFAULT" if status == "completed" else "ERROR",
        "langfuse.trace.name": trace_name,
        "langfuse.trace.public": False,
        "langfuse.observation.metadata.invocation_id": trace_id,
        "langfuse.observation.metadata.provider": _identifier(receipt.get("provider")),
        "langfuse.observation.metadata.stage": stage,
        "langfuse.observation.metadata.status": status,
        "langfuse.observation.metadata.actual_provider_calls": _count(receipt.get("actual_provider_calls")),
        "langfuse.observation.metadata.assignment_model_cost_usd": ASSIGNMENT_MODEL_COST_USD,
        "langfuse.observation.metadata.actual_cost_status": "known" if cost is not None else "unknown",
        "langfuse.observation.metadata.actual_cost_usd": cost if cost is not None else "unknown",
        "langfuse.observation.metadata.actual_cost_source": "provider_response_usage.cost" if cost is not None else "not_returned_by_provider",
        "langfuse.observation.metadata.content_captured": False,
    }
    for key, value in context.items():
        values[f"langfuse.observation.metadata.{key}"] = value
    prompt_hash = receipt.get("prompt_hash")
    if isinstance(prompt_hash, str) and re.fullmatch(r"[0-9a-f]{64}", prompt_hash):
        values["langfuse.observation.metadata.prompt_hash"] = prompt_hash
    latency = actual_cost(receipt.get("latency_ms"))
    if latency is not None:
        values["langfuse.observation.metadata.latency_ms"] = latency
    error_type = _identifier(receipt.get("error_type"))
    if error_type:
        values["langfuse.observation.metadata.error_type"] = error_type
    http_status = _count(receipt.get("http_status"))
    if http_status is not None:
        values["langfuse.observation.metadata.http_status"] = http_status
    for key, value in inclusive.items():
        values[f"langfuse.observation.metadata.provider_{key}"] = value
    if model:
        values["langfuse.observation.model.name"] = model
    if usage:
        values["langfuse.observation.usage_details"] = json.dumps(usage, separators=(",", ":"))
    if cost is not None:
        values["langfuse.observation.cost_details"] = json.dumps({"total": cost}, separators=(",", ":"))
    span = {
        "traceId": trace_id, "spanId": span_id, "name": trace_name, "kind": 3,
        "startTimeUnixNano": _timestamp_ns(receipt["started_at"]),
        "endTimeUnixNano": _timestamp_ns(receipt["finished_at"]),
        "status": {"code": 1 if status == "completed" else 2},
        "attributes": [_attribute(key, value) for key, value in values.items() if value is not None],
    }
    return {"resourceSpans": [{
        "resource": {"attributes": [_attribute("service.name", "vivi-agent")]},
        "scopeSpans": [{"scope": {"name": "vivi.provider", "version": "1"}, "spans": [span]}],
    }]}


def _local_url(base_url):
    url = urlsplit(base_url)
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment):
        raise ValueError("Invalid local Langfuse URL")
    hostname = url.hostname.lower()
    if hostname not in _LOCAL_HOSTS:
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            raise ValueError("Langfuse must use the local self-hosted endpoint") from None
        if not address.is_loopback and not any(address in network for network in _PRIVATE_NETWORKS):
            raise ValueError("Langfuse must use the local self-hosted endpoint")
    # URL을 노출하지 않고 port 접근으로 포트 형식과 범위를 검증한다.
    url.port
    return url


def _post(url, public_key, secret_key, payload):
    connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(url.hostname, url.port, timeout=EXPORT_TIMEOUT_SECONDS)
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    try:
        connection.request("POST", url.path.rstrip("/") + "/api/public/otel/v1/traces",
                           body=json.dumps(payload, separators=(",", ":")).encode(), headers={
                               "Content-Type": "application/json", "Authorization": f"Basic {token}",
                           })
        response = connection.getresponse()
        # HTTPConnection은 리다이렉트를 따라가지 않아 인증 정보가 외부로 전달되지 않는다.
        if response.status != 200:
            return {"enabled": True, "status": "failed", "http_status": response.status}
        body = response.read(64 * 1024)
        result = json.loads(body) if body else {}
        partial = result.get("partialSuccess", {})
        if partial and (int(partial.get("rejectedSpans", 0)) > 0 or partial.get("errorMessage")):
            return {"enabled": True, "status": "failed", "error_type": "PartialIngestion"}
        return {"enabled": True, "status": "accepted"}
    finally:
        connection.close()


def emit_generation(receipt):
    """로컬 전송을 한 번 시도하며 관측 실패를 호출자에게 전파하지 않는다."""
    try:
        base_url = os.environ.get("LANGFUSE_BASE_URL", "").strip()
        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
        if not any((base_url, public_key, secret_key)):
            return {"enabled": False, "status": "disabled"}
        if not all((base_url, public_key, secret_key)):
            return {"enabled": False, "status": "configuration_incomplete"}
        if _count(receipt.get("actual_provider_calls")) != 1:
            return {"enabled": True, "status": "not_attempted"}
        url = _local_url(base_url)
        payload = generation_payload(receipt)
        result = _post(url, public_key, secret_key, payload)
        span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        return {**result, "trace_id": span["traceId"], "observation_id": span["spanId"]}
    except Exception as exc:
        # URL·인증 헤더·원격 오류 본문·예외 원문은 남기지 않는다.
        return {"enabled": True, "status": "failed", "error_type": type(exc).__name__}
