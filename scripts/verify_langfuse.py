#!/usr/bin/env python3
"""지정한 공급사 영수증만 자체 호스팅 Langfuse 기록과 대사한다.

Langfuse v3.225.11 공개 API를 사용한다. 인증 정보, 요청 헤더, 응답 본문,
프롬프트, 출력, 원격 오류 메시지를 출력하지 않는다.
모델을 호출하거나 Langfuse 기록을 변경하지 않는다.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import http.client
import ipaddress
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urlsplit

from setup_langfuse import read_values


ROOT = Path(__file__).resolve().parents[1]


class ApiError(Exception):
    def __init__(self, kind: str, status: int | None = None):
        self.kind, self.status = kind, status


class Client:
    def __init__(self, values: dict[str, str]):
        self.url = urlsplit(values.get('LANGFUSE_BASE_URL', ''))
        if (self.url.scheme not in {'http', 'https'} or not self.url.hostname
                or self.url.username is not None or self.url.password is not None
                or self.url.query or self.url.fragment):
            raise ApiError('invalid_local_endpoint')
        hostname = self.url.hostname.lower()
        if hostname not in {'localhost', 'langfuse-web', 'langfuse', 'host.docker.internal'}:
            try:
                ip = ipaddress.ip_address(hostname)
            except ValueError:
                raise ApiError('nonlocal_endpoint_rejected') from None
            if not ip.is_loopback and not any(ip in ipaddress.ip_network(net) for net in
                                              ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7']):
                raise ApiError('nonlocal_endpoint_rejected')
        if not values.get('LANGFUSE_PUBLIC_KEY') or not values.get('LANGFUSE_SECRET_KEY'):
            raise ApiError('missing_api_credentials')
        self.authorization = 'Basic ' + base64.b64encode(
            f"{values['LANGFUSE_PUBLIC_KEY']}:{values['LANGFUSE_SECRET_KEY']}".encode()).decode()

    def get(self, path: str, query: dict | None = None) -> dict:
        connection_type = http.client.HTTPSConnection if self.url.scheme == 'https' else http.client.HTTPConnection
        connection = connection_type(self.url.hostname, self.url.port, timeout=10)
        try:
            suffix = '?' + urlencode(query) if query else ''
            connection.request('GET', self.url.path.rstrip('/') + path + suffix,
                               headers={'Authorization': self.authorization})
            response = connection.getresponse()
            if response.status != 200:
                raise ApiError('http_error', response.status)
            body = response.read(4 * 1024 * 1024)
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ApiError('unexpected_response_shape')
            return result
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(type(exc).__name__) from None
        finally:
            connection.close()


def number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def close_number(left, right, tolerance='0.0000000001') -> bool:
    if not number(left) or not number(right):
        return False
    try:
        return abs(Decimal(str(left)) - Decimal(str(right))) <= Decimal(tolerance)
    except InvalidOperation:
        return False


def timestamp_delta_ms(start, end):
    try:
        return (datetime.fromisoformat(end.replace('Z', '+00:00'))
                - datetime.fromisoformat(start.replace('Z', '+00:00'))).total_seconds() * 1000
    except (AttributeError, TypeError, ValueError):
        return None


def reconcile(path: Path, result: dict, client: Client) -> dict:
    receipt = result.get('validation_receipt')
    if not isinstance(receipt, dict):
        return {'file': str(path), 'passed': False, 'errors': ['missing_validation_receipt']}
    observability = receipt.get('observability') or {}
    trace_id, observation_id = observability.get('trace_id'), observability.get('observation_id')
    report = {'file': str(path), 'topic_id': result.get('topic_id'),
              'product_result_type': result.get('result_type'),
              'invocation_id': receipt.get('invocation_id'), 'trace_id': trace_id,
              'observation_id': observation_id, 'export_status': observability.get('status')}
    if not isinstance(trace_id, str) or not re.fullmatch('[0-9a-f]{32}', trace_id):
        return {**report, 'passed': False, 'errors': ['missing_or_invalid_trace_id']}
    if not isinstance(observation_id, str) or not re.fullmatch('[0-9a-f]{16}', observation_id):
        return {**report, 'passed': False, 'errors': ['missing_or_invalid_observation_id']}
    try:
        listing = client.get('/api/public/observations', {'traceId': trace_id, 'type': 'GENERATION', 'limit': 100, 'page': 1})
        observation = client.get('/api/public/observations/' + observation_id)
        trace = client.get('/api/public/traces/' + trace_id, {'fields': 'core,observations,metrics'})
    except ApiError as exc:
        return {**report, 'passed': False, 'pending': exc.status == 404,
                'errors': [exc.kind], 'http_status': exc.status}

    rows = listing.get('data') or []
    generations = [row for row in rows if row.get('type') == 'GENERATION']
    total = (listing.get('meta') or {}).get('totalItems', len(rows))
    metadata = observation.get('metadata') or {}
    usage = observation.get('usageDetails') or {}
    cost = (observation.get('costDetails') or {}).get('total')
    raw_usage = receipt.get('usage') or {}
    expected_cost = receipt.get('actual_cost_usd')
    expected_status = 'completed' if receipt.get('status') == 'completed' else 'failed'
    expected_model = receipt.get('model') or receipt.get('requested_model')
    expected_context = receipt.get('observation_context') or {}
    actual_input = usage.get('input', 0) + usage.get('input_cached_tokens', 0)
    actual_output = usage.get('output', 0) + usage.get('output_reasoning_tokens', 0)
    expected_input, expected_output = raw_usage.get('input_tokens'), raw_usage.get('output_tokens')
    expected_total = raw_usage.get('total_tokens')
    if expected_total is None and type(expected_input) is int and type(expected_output) is int:
        expected_total = expected_input + expected_output
    expected_cached = (raw_usage.get('input_tokens_details') or {}).get('cached_tokens')
    expected_reasoning = (raw_usage.get('output_tokens_details') or {}).get('reasoning_tokens')
    checks = {
        'one_provider_call': receipt.get('actual_provider_calls') == 1,
        'one_generation_in_trace': total == 1 and len(generations) == 1 and generations[0].get('id') == observation_id,
        'observation_identity': observation.get('id') == observation_id and observation.get('traceId') == trace_id,
        'trace_identity': trace.get('id') == trace_id,
        'generation_type': observation.get('type') == 'GENERATION',
        'model': observation.get('model') == expected_model,
        'status': metadata.get('status') == expected_status,
        'level': observation.get('level') == ('DEFAULT' if expected_status == 'completed' else 'ERROR'),
        'topic_metadata': metadata.get('topic_id') == result.get('topic_id') == expected_context.get('topic_id'),
        'invocation_metadata': metadata.get('invocation_id') == trace_id,
        'call_count_metadata': metadata.get('actual_provider_calls') == 1,
        'input_tokens': type(expected_input) is int and actual_input == expected_input,
        'output_tokens': type(expected_output) is int and actual_output == expected_output,
        'total_tokens': type(expected_total) is int and usage.get('total') == expected_total,
        'cost_known': number(expected_cost) and expected_cost >= 0,
        'actual_cost_details': close_number(cost, expected_cost),
        'trace_total_cost': close_number(trace.get('totalCost'), expected_cost),
        'cost_metadata': close_number(metadata.get('actual_cost_usd'), expected_cost),
        'cost_source': metadata.get('actual_cost_source') == 'provider_response_usage.cost' and metadata.get('actual_cost_status') == 'known',
        'latency_metadata': close_number(metadata.get('latency_ms'), receipt.get('latency_ms'), '0.001'),
        'latency_duration': close_number(timestamp_delta_ms(observation.get('startTime'), observation.get('endTime')), receipt.get('latency_ms'), '5'),
        'content_not_captured': observation.get('input') is None and observation.get('output') is None and metadata.get('content_captured') is False,
    }
    if expected_cached is not None:
        checks['cached_tokens'] = usage.get('input_cached_tokens', 0) == expected_cached
    if isinstance(receipt.get('invocation_id'), str) and re.fullmatch('[0-9a-f]{32}', receipt['invocation_id']):
        checks['receipt_invocation_identity'] = receipt['invocation_id'] == trace_id
    if expected_reasoning is not None:
        checks['reasoning_tokens'] = usage.get('output_reasoning_tokens', 0) == expected_reasoning
    for key in ('snapshot_id', 'question_id', 'dataset_version', 'policy_version', 'run_key_hash'):
        if key in expected_context:
            checks[key + '_metadata'] = metadata.get(key) == expected_context[key]
    return {**report, 'passed': all(checks.values()), 'checks': checks,
            'errors': [key for key, passed in checks.items() if not passed],
            'generation_count': total, 'model': observation.get('model'),
            'provider_receipt_status': receipt.get('status'),
            'generation_status': metadata.get('status'), 'level': observation.get('level'),
            'receipt_usage': {'input_tokens': expected_input, 'output_tokens': expected_output,
                              'total_tokens': expected_total, 'cached_tokens': expected_cached,
                              'reasoning_tokens': expected_reasoning},
            'observed_usage_details': usage,
            'receipt_cost_usd': expected_cost, 'observed_cost_details': observation.get('costDetails'),
            'trace_total_cost_usd': trace.get('totalCost'),
            'receipt_latency_ms': receipt.get('latency_ms'),
            'observed_latency_ms': timestamp_delta_ms(observation.get('startTime'), observation.get('endTime')),
            'observed_api_latency_seconds': observation.get('latency'),
            'metadata': {key: metadata.get(key) for key in ('topic_id', 'snapshot_id', 'question_id', 'policy_version', 'actual_cost_source', 'content_captured')},
            'trace_url': client.url.geturl().rstrip('/') + trace.get('htmlPath', '')}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+', type=Path, help='Explicit real-run JSON files; no global trace totals are queried.')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--output', type=Path, default=ROOT / 'output/product-v2-langfuse.json')
    parser.add_argument('--wait-seconds', type=float, default=0, help='Bounded retry for asynchronous observation availability, at most 300 seconds.')
    args = parser.parse_args()
    if any(not path.is_file() for path in args.files):
        print('Verification not started: an explicit input file is missing.')
        return 2
    try:
        client = Client(read_values(args.env_file.read_text()))
        server_version = client.get('/api/public/health').get('version')
        inputs = [(path.resolve(), json.loads(path.read_text())) for path in args.files]
        if any(not isinstance(data, dict) for _, data in inputs):
            raise ValueError
    except ApiError as exc:
        print('Verification configuration error: ' + exc.kind)
        return 2
    except Exception as exc:
        print('Verification input error: ' + type(exc).__name__)
        return 2
    deadline = time.monotonic() + min(max(args.wait_seconds, 0), 300)
    while True:
        results = [reconcile(path, value, client) for path, value in inputs]
        if all(row['passed'] for row in results) or time.monotonic() >= deadline:
            break
        # 같은 ID의 기록만 다시 조회한다. 모델 호출은 재전송하지 않는다.
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    trace_ids = [row.get('trace_id') for row in results]
    unique_invocations = len(trace_ids) == len(set(trace_ids)) and None not in trace_ids
    costs = [Decimal(str(row['receipt_cost_usd'])) for row in results if number(row.get('receipt_cost_usd'))]
    observed = [Decimal(str(row['trace_total_cost_usd'])) for row in results if number(row.get('trace_total_cost_usd'))]
    report = {
        'kind': 'specific_real_invocation_langfuse_receipt_reconciliation',
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'server_version': server_version,
        'source_contract': client.url.geturl().rstrip('/') + '/generated/api/openapi.yml',
        'scope': 'Only the explicit input receipt trace IDs; unrelated and synthetic test traces are excluded.',
        'passed': unique_invocations and all(row['passed'] for row in results),
        'unique_invocations': unique_invocations,
        'case_count': len(results), 'passed_cases': sum(row['passed'] for row in results),
        'receipt_total_actual_cost_usd': float(sum(costs, Decimal(0))) if len(costs) == len(results) else None,
        'langfuse_total_actual_cost_usd': float(sum(observed, Decimal(0))) if len(observed) == len(results) else None,
        'cases': results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f"Langfuse receipt reconciliation: {report['passed_cases']}/{len(results)} passed; unique invocations: {unique_invocations}.")
    print(f'Report: {args.output.resolve()}')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
