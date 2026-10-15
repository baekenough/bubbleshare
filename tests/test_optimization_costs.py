"""앱 코드·인증 정보·유료 호출 없이 비용 계산을 오프라인으로 검증한다."""
from copy import deepcopy
from decimal import Decimal as D
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location('optimization_costs', Path(__file__).parents[1] / 'scripts/analyze_optimization_costs.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


PRICES = {'openai/gpt-5.4-mini': {'prompt': '0.00000075', 'input_cache_read': '0.000000075', 'completion': '0.0000045'}}


def artifact(cost='0.00525', invocation='id-one', status='completed', result='propose', cached=0):
    prompt_cost = D(1000 - cached) * D('.00000075') + D(cached) * D('.000000075')
    receipt = {'invocation_id': invocation, 'actual_provider_calls': 1,
               'actual_cost_usd': cost, 'status': status, 'model': 'openai/gpt-5.4-mini',
               'usage': {'input_tokens': 1000, 'output_tokens': 1000,
                         'input_tokens_details': {'cached_tokens': cached},
                         'output_tokens_details': {'reasoning_tokens': 400}},
               'raw_usage': {'cost': cost, 'cost_details': {
                   'upstream_inference_prompt_cost': str(prompt_cost),
                   'upstream_inference_completions_cost': '0.0045'}}}
    return {'result_type': result, 'usage': {'model_calls': 1}, 'validation_receipt': receipt}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


def normalized(tmp_path, name='lead', data=None, **kwargs):
    data = data if data is not None else artifact()
    path = write(tmp_path / (name + '.json'), data)
    return MODULE.normalize(path, data, PRICES, {}, **kwargs)


def test_cost_parts_use_reported_totals_and_disjoint_token_subsets(tmp_path):
    row = normalized(tmp_path, data=artifact(cost='0.00471', cached=800))
    assert row['actual_cost_usd'] == D('.00471')
    assert row['tokens'] == {'input_including_cached': 1000, 'input_uncached': 200, 'input_cached': 800,
                             'output_including_reasoning': 1000, 'output_nonreasoning': 600, 'output_reasoning': 400}
    parts = row['derived_token_cost_parts_usd']
    assert parts == {'input_uncached': D('.00015'), 'input_cached': D('.00006'),
                     'output_reasoning': D('.0018'), 'output_nonreasoning': D('.0027')}
    assert sum(parts.values()) == row['actual_cost_usd']


def test_failed_paid_attempt_cost_is_included(tmp_path):
    rows = [normalized(tmp_path, 'lead', artifact('0.03', 'id-1')),
            normalized(tmp_path, 'security', artifact('0.02', 'id-2', status='failed', result='fallback'))]
    summary = MODULE.aggregate(rows)
    assert summary['total_actual_cost_usd'] == D('.05')
    assert summary['mean_per_paid_call_usd'] == D('.025')
    assert summary['provider_failed_attempts'] == summary['product_fallback_attempts'] == 1


@pytest.mark.parametrize('cost', [None, -1, 'NaN', True, 'not-a-charge'])
def test_unknown_cost_never_becomes_zero_or_usable_projection(tmp_path, cost):
    rows = [normalized(tmp_path, 'lead', artifact('0.03', 'id-1')),
            normalized(tmp_path, 'security', artifact(cost, 'id-2', status='failed', result='fallback'))]
    summary = MODULE.aggregate(rows)
    assert summary['known_actual_cost_subtotal_usd'] == D('.03')
    assert summary['unknown_cost_attempts'] == 1
    assert summary['total_actual_cost_usd'] is None
    assert summary['mean_per_paid_call_usd'] is None
    p = MODULE.margin_projection(summary['mean_per_paid_call_usd'], D(0))
    assert p['months']['average_4_3_weeks']['maximum_fresh_of_1000'] is None
    assert p['months']['average_4_3_weeks']['scenarios'][0]['gross_margin_pct'] is None


def test_zero_call_request_does_not_dilute_fresh_paid_call_cost(tmp_path):
    rows = [normalized(tmp_path, 'lead', artifact('0.03', 'id-1')),
            normalized(tmp_path, 'cached', {'result_type': 'cache_hit', 'usage': {'model_calls': 0}})]
    summary = MODULE.aggregate(rows)
    assert summary['mean_cost_per_observed_request_usd'] == D('.015')
    assert summary['mean_per_paid_call_usd'] == D('.03')
    assert summary['zero_call_requests'] == 1


def test_baseline_uses_source_generation_not_zero_cost_recompile(tmp_path):
    data = artifact('0.185')
    data['source_generation_receipt'] = data.pop('validation_receipt')
    data['usage'] = {'model_calls': 0, 'actual_vendor_cost_usd': 0}
    data['revalidation'] = {'new_model_calls': 0}
    row = normalized(tmp_path, data=data, baseline=True)
    assert row['baseline_uses_original_generation'] is True
    assert row['provider_calls'] == 1 and row['actual_cost_usd'] == D('.185')


def test_duplicate_invocation_deduplication_and_conflict(tmp_path):
    original = normalized(tmp_path, 'lead', artifact('0.03', 'same-id'))
    duplicate = normalized(tmp_path, 'copy', artifact('0.03', 'same-id'))
    assert MODULE.aggregate([original, duplicate])['total_actual_cost_usd'] == D('.03')
    assert MODULE.aggregate([original, duplicate])['duplicate_artifact_count'] == 1
    conflict = deepcopy(duplicate)
    conflict['actual_cost_usd'] = D('.04')
    with pytest.raises(ValueError, match='Conflicting'):
        MODULE.aggregate([original, conflict])


def test_80_percent_margin_arithmetic_and_langfuse_increment(tmp_path):
    p = MODULE.margin_projection(D('.025'), D(0))
    average, five = p['months']['average_4_3_weeks'], p['months']['five_collection_rounds']
    assert average['scenarios'][0]['model_only_cost_usd'] == D(25)
    assert average['scenarios'][0]['gross_margin_pct'] == D('65.13')
    assert average['scenarios'][1]['full_cogs_usd'] == D('22.37')
    assert average['maximum_fresh_of_1000'] == 405
    assert five['maximum_fresh_of_1000'] == 380
    assert MODULE.margin_projection(D('.025'), D(1))['months']['average_4_3_weeks']['maximum_fresh_of_1000'] == 365
    assert MODULE.margin_projection(D('.018'), D(0))['months']['average_4_3_weeks']['maximum_fresh_of_1000'] == 562
    assert MODULE.margin_projection(D('.018'), D(0))['months']['five_collection_rounds']['maximum_fresh_of_1000'] == 527


def test_quality_is_not_inferred_and_digest_mismatch_cannot_pass(tmp_path):
    path = write(tmp_path / 'lead.json', artifact())
    assert MODULE.normalize(path, MODULE.read_json(path), PRICES, {})['quality'] is None
    audit_path = write(tmp_path / 'audit.json', {'reviewer': 'independent reviewer', 'cases': [
        {'artifact': str(path), 'artifact_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'passed': True, 'total': 9}]})
    index = MODULE.quality_index([audit_path])
    assert MODULE.audited_quality(path, index)['passed'] is True
    path.write_text(path.read_text() + '\n')
    stale = MODULE.quality_index([audit_path])
    assert MODULE.audited_quality(path, stale)['passed'] is None


def test_missing_summary_attempt_is_unknown_not_dropped(tmp_path):
    normalized(tmp_path, 'lead', artifact('0.03', 'id-1'))
    write(tmp_path / 'execution-summary.json', {'model_calls': 2, 'cases': [
        {'case': 'lead', 'model_calls': 1}, {'case': 'security', 'model_calls': 1,
                                        'result_type': 'fallback', 'actual_vendor_cost_usd': '0.02'}]})
    rows, completeness = MODULE.experiment_rows(tmp_path, PRICES, {})
    assert completeness['missing_summary_attempt_artifacts'] == ['security']
    summary = MODULE.aggregate(rows)
    assert summary['known_provider_calls'] == 2
    assert summary['known_actual_cost_subtotal_usd'] == D('.03')
    assert summary['total_actual_cost_usd'] is None


def test_raw_trace_format_and_mismatched_costs(tmp_path):
    data = artifact('0.01')
    trace = {'receipt': data['validation_receipt'], 'provider_response': {'usage': data['validation_receipt']['raw_usage']}}
    row = normalized(tmp_path, data=trace)
    assert row['receipt_json_pointer'] == '/receipt' and row['actual_cost_usd'] == D('.01')
    trace['receipt']['actual_cost_usd'] = '0.02'
    row = normalized(tmp_path, 'mismatch', trace)
    assert row['cost_status'] == 'unknown'
    assert 'receipt_cost_disagrees_with_raw_provider_usage' in row['accounting_issues']


def test_build_report_includes_baseline_provenance_every_folder_and_separate_assignment(tmp_path):
    baseline, first, second = (tmp_path / name for name in ('baseline', 'first', 'second'))
    for name in MODULE.BASELINE_CASES:
        base = artifact('0.2', 'base-' + name)
        base['source_generation_receipt'] = base.pop('validation_receipt')
        base['usage']['model_calls'] = 0
        write(baseline / (name + '.json'), base)
        write(first / (name + '.json'), artifact('0.025', 'first-' + name))
    write(second / 'lead.json', artifact(None, 'second-lead', status='failed', result='fallback'))
    catalog = write(tmp_path / 'prices.json', {'models': [{'id': key, 'pricing': value} for key, value in PRICES.items()]})
    report = MODULE.build_report(baseline, [first, second], catalog)
    assert report['baseline']['summary']['total_actual_cost_usd'] == D('.8')
    assert report['experiments'][0]['four_case_total_savings_vs_baseline']['saving_pct'] == D('87.5')
    assert report['experiments'][0]['completeness']['folder_run_complete'] is False
    assert report['all_experiment_attempts_summary']['known_provider_calls'] == 5
    assert report['all_experiment_attempts_summary']['total_actual_cost_usd'] is None
    assert report['assignment_accounting']['fixed_unit_cost_usd'] == D('.018')
    assert len(report['baseline']['attempts'][0]['artifact_sha256']) == 64
