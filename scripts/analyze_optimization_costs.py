#!/usr/bin/env python3
"""저장된 기준·실험 영수증을 비교한다. 모델·네트워크·환경 변수에 접근하지 않는다.

실행 예:
  python3 scripts/analyze_optimization_costs.py output/cost-compact-mini-low --quality-audit output/product-v2-quality-audit.json

실패와 무호출 요청도 포함한다. 비용 미확인 시 총액·평균·예상치는 null로 둔다.
동일한 공급사 호출 ID는 한 번만 집계하며, 품질은 명시적인 감사 결과로만 판단한다.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, ROUND_FLOOR, localcontext
import hashlib
import json
from pathlib import Path
import statistics


ROOT = Path(__file__).resolve().parents[1]
D = Decimal
ZERO = D(0)
BASELINE_CASES = ('lead', 'unsupported', 'security', 'confirmed')


def read_json(path):
    return json.loads(Path(path).read_text(), parse_float=D)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolved(value):
    path = Path(value)
    if not path.is_absolute():
        return ROOT / path
    if not path.exists():
        for directory in ('output', '.vivi'):
            if directory in path.parts:
                candidate = ROOT.joinpath(*path.parts[path.parts.index(directory):])
                if candidate.exists():
                    return candidate
    return path


def label(path):
    path = Path(path).resolve()
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def money(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = D(str(value))
    except (ArithmeticError, ValueError, TypeError):
        return None
    return number if number.is_finite() and number >= 0 else None


def count(value):
    return value if type(value) is int and value >= 0 else None


def ratio(numerator, denominator):
    if numerator is None or denominator in (None, 0):
        return None
    with localcontext() as context:
        context.prec = 40
        return D(numerator) / D(denominator)


def saving(before, after):
    if before is None or after is None:
        return {'saved_usd': None, 'saving_pct': None}
    return {'saved_usd': before - after, 'saving_pct': ratio(before - after, before) * 100 if before else None}


def quality_index(paths):
    index = {}
    for path in paths:
        document = read_json(path)
        for case in document.get('cases', []):
            if not isinstance(case, dict) or not (case.get('artifact') or case.get('artifact_path')):
                continue
            artifact = resolved(case.get('artifact') or case['artifact_path']).resolve()
            valid = artifact.is_file() and (not case.get('artifact_sha256') or sha(artifact) == case['artifact_sha256'])
            index.setdefault(str(artifact), []).append({
                'audit_file': label(path), 'audit_sha256': sha(path),
                'passed': case.get('passed') if valid and isinstance(case.get('passed'), bool) else None,
                'score': case.get('total'), 'reviewer': document.get('reviewer'),
                'reported_outcomes': {key: case.get(key) for key in ('base_rubric_pass', 'baseline_maintained', 'adoptable') if key in case},
                'artifact_verification': ('sha256_matches' if case.get('artifact_sha256') else 'path_only') if valid else 'artifact_hash_mismatch_or_missing',
                'findings': case.get('findings', []), 'scope_limitations': document.get('scope_limitations', document.get('limitations', [])),
            })
    return index


def audited_quality(path, index):
    audits = index.get(str(Path(path).resolve()), [])
    if not audits:
        return None
    values = {audit['passed'] for audit in audits}
    return {'passed': next(iter(values)) if len(values) == 1 else None,
            'status': 'available' if len(values) == 1 and None not in values else 'unverified_or_conflicting',
            'audits': audits}


def catalog_prices(path):
    data = read_json(path)
    return {model['id']: model['pricing'] for model in data['models']}


def normalize(path, artifact, prices, audits, *, baseline=False):
    field = 'source_generation_receipt' if baseline and 'source_generation_receipt' in artifact else 'validation_receipt'
    receipt = artifact.get(field)
    if not isinstance(receipt, dict) and isinstance(artifact.get('receipt'), dict):
        field, receipt = 'receipt', artifact['receipt']
    receipt = receipt if isinstance(receipt, dict) else {}
    usage = receipt.get('usage') if isinstance(receipt.get('usage'), dict) else {}
    raw = receipt.get('raw_usage') if isinstance(receipt.get('raw_usage'), dict) else {}
    if not raw and isinstance(artifact.get('provider_response'), dict):
        raw = artifact['provider_response'].get('usage') or {}
    request_usage = artifact.get('usage') if isinstance(artifact.get('usage'), dict) else {}
    calls = count(receipt.get('actual_provider_calls'))
    if calls is None:
        calls = count(request_usage.get('model_calls'))
    total_cost, basis = None, 'unknown'
    issues = []
    if calls == 0:
        observed = money(receipt.get('actual_cost_usd')) or money(raw.get('cost'))
        if observed:
            issues.append('zero_call_count_with_positive_provider_charge')
        else:
            total_cost, basis = ZERO, 'explicit_zero_provider_calls'
    elif calls == 1:
        direct, raw_cost = money(receipt.get('actual_cost_usd')), money(raw.get('cost'))
        if direct is not None and raw_cost is not None and direct != raw_cost:
            issues.append('receipt_cost_disagrees_with_raw_provider_usage')
        elif direct is not None:
            total_cost, basis = direct, 'receipt.actual_cost_usd'
        elif receipt.get('actual_cost_usd') is None and raw_cost is not None:
            total_cost, basis = raw_cost, 'receipt.raw_usage.cost'
    if calls not in (0, 1):
        issues.append('missing_or_non_single_call_receipt')
    input_count = count(usage.get('input_tokens', raw.get('prompt_tokens')))
    output_count = count(usage.get('output_tokens', raw.get('completion_tokens')))
    input_details = usage.get('input_tokens_details') or raw.get('prompt_tokens_details') or {}
    output_details = usage.get('output_tokens_details') or raw.get('completion_tokens_details') or {}
    cached = count(input_details.get('cached_tokens'))
    reasoning = count(output_details.get('reasoning_tokens'))
    if cached is not None and (input_count is None or cached > input_count):
        cached = None
        issues.append('invalid_cached_token_count')
    if reasoning is not None and (output_count is None or reasoning > output_count):
        reasoning = None
        issues.append('invalid_reasoning_token_count')
    tokens = {'input_including_cached': input_count, 'input_cached': cached,
              'input_uncached': input_count - cached if input_count is not None and cached is not None else None,
              'output_including_reasoning': output_count, 'output_reasoning': reasoning,
              'output_nonreasoning': output_count - reasoning if output_count is not None and reasoning is not None else None}
    details = raw.get('cost_details') or {}
    prompt_cost = money(details.get('upstream_inference_prompt_cost'))
    output_cost = money(details.get('upstream_inference_completions_cost'))
    derived = {'input_uncached': None, 'input_cached': None, 'output_nonreasoning': None, 'output_reasoning': None}
    model = receipt.get('model') or receipt.get('requested_model')
    price = prices.get(model) or {}
    if prompt_cost is not None and cached == 0:
        derived.update(input_uncached=prompt_cost, input_cached=ZERO)
    elif prompt_cost is not None and cached is not None and tokens['input_uncached'] is not None:
        cold_rate, cache_rate = money(price.get('prompt')), money(price.get('input_cache_read'))
        if cold_rate is not None and cache_rate is not None:
            cold, warm = D(tokens['input_uncached']) * cold_rate, D(cached) * cache_rate
            if cold + warm == prompt_cost:
                derived.update(input_uncached=cold, input_cached=warm)
    if output_cost is not None and reasoning is not None and output_count is not None:
        if output_count:
            reasoning_cost = ratio(output_cost, output_count) * D(reasoning)
            derived.update(output_reasoning=reasoning_cost, output_nonreasoning=output_cost - reasoning_cost)
        elif output_cost == 0:
            derived.update(output_reasoning=ZERO, output_nonreasoning=ZERO)
    original = (artifact.get('revalidation') or {}).get('original_file')
    original_path = resolved(original) if original else None
    trace_path = resolved(receipt['trace_path']) if receipt.get('trace_path') else None
    invocation = receipt.get('invocation_id')
    if not isinstance(invocation, str) or not invocation:
        invocation = None
    return {
        'case': path.stem, 'artifact': label(path), 'artifact_sha256': sha(path),
        'receipt_json_pointer': '/' + field if receipt else None,
        'original_generation_artifact': label(original_path) if original_path else None,
        'original_generation_sha256': sha(original_path) if original_path and original_path.is_file() else None,
        'trace_path': label(trace_path) if trace_path else None,
        'trace_sha256': sha(trace_path) if trace_path and trace_path.is_file() else None,
        'invocation_id': invocation, 'deduplication_key': invocation or 'unverified:' + str(path.resolve()),
        'model': model, 'generation_profile': receipt.get('generation_profile') or {
            'reasoning_effort': receipt.get('reasoning_effort'), 'max_output_tokens': receipt.get('max_output_tokens')},
        'product_result_type': artifact.get('result_type'), 'provider_status': receipt.get('status'),
        'provider_calls': calls, 'cost_status': 'known' if total_cost is not None else 'unknown',
        'actual_cost_usd': total_cost, 'cost_basis': basis, 'tokens': tokens,
        'reported_cost_parts_usd': {'prompt_including_cached': prompt_cost, 'completion_including_reasoning': output_cost},
        'derived_token_cost_parts_usd': derived,
        'unallocated_provider_cost_usd': total_cost - prompt_cost - output_cost if all(v is not None for v in (total_cost, prompt_cost, output_cost)) else None,
        'quality': audited_quality(path, audits), 'accounting_issues': issues,
        'baseline_uses_original_generation': baseline and field == 'source_generation_receipt',
        'new_calls_for_recompilation': (artifact.get('revalidation') or {}).get('new_model_calls'),
    }


def unique_attempts(rows):
    unique = {}
    for row in rows:
        prior = unique.get(row['deduplication_key'])
        if prior and (prior['actual_cost_usd'], prior['provider_calls'], prior['tokens']) != (row['actual_cost_usd'], row['provider_calls'], row['tokens']):
            raise ValueError('Conflicting records for one provider invocation')
        unique.setdefault(row['deduplication_key'], row)
    return list(unique.values())


def values_summary(values):
    known = [value for value in values if value is not None]
    return {'known_subtotal': sum(known, 0), 'unknown_items': len(values) - len(known),
            'total': sum(known, 0) if len(known) == len(values) else None}


def aggregate(rows):
    unique = unique_attempts(rows)
    paid = [row for row in unique if row['provider_calls'] != 0]
    costs = values_summary([row['actual_cost_usd'] for row in unique])
    paid_calls = sum(row['provider_calls'] or 0 for row in paid)
    uncertain_calls = sum(row['provider_calls'] is None for row in unique)
    complete = costs['unknown_items'] == 0 and uncertain_calls == 0
    means = ratio(costs['total'], paid_calls) if complete else None
    return {'artifact_count': len(rows), 'unique_invocation_or_unverified_attempt_count': len(unique),
            'duplicate_artifact_count': len(rows) - len(unique), 'known_provider_calls': paid_calls,
            'unknown_call_count_records': uncertain_calls, 'zero_call_requests': sum(row['provider_calls'] == 0 for row in unique),
            'provider_failed_attempts': sum(row['provider_status'] == 'failed' for row in paid),
            'product_fallback_attempts': sum(row['product_result_type'] == 'fallback' for row in unique),
            'cost_accounting_complete': complete, 'known_actual_cost_subtotal_usd': costs['known_subtotal'],
            'unknown_cost_attempts': costs['unknown_items'], 'total_actual_cost_usd': costs['total'] if complete else None,
            'mean_per_paid_call_usd': means,
            'median_paid_attempt_usd': statistics.median([row['actual_cost_usd'] for row in paid]) if complete and paid else None,
            'mean_cost_per_observed_request_usd': ratio(costs['total'], len(unique)) if complete else None,
            'token_counts': {key: values_summary([row['tokens'][key] for row in paid]) for key in
                             ('input_including_cached', 'input_cached', 'input_uncached', 'output_including_reasoning', 'output_reasoning', 'output_nonreasoning')},
            'reported_cost_parts_usd': {key: values_summary([row['reported_cost_parts_usd'][key] for row in paid])
                                       for key in ('prompt_including_cached', 'completion_including_reasoning')},
            'derived_token_cost_parts_usd': {key: values_summary([row['derived_token_cost_parts_usd'][key] for row in paid])
                                            for key in ('input_uncached', 'input_cached', 'output_nonreasoning', 'output_reasoning')}}


def margin_projection(unit, h):
    result = {'per_fresh_request_model_cost_usd': unit, 'incremental_langfuse_h_usd': h,
              'basis': 'One new provider call per fresh request; observed paid-call mean includes paid failures. This is a sample projection, not a price ceiling or quality guarantee.',
              'months': {}}
    for month, fixed in [('average_4_3_weeks', D('9.87')), ('five_collection_rounds', D('10.5'))]:
        available = D(20) - fixed - h
        cap = None if unit is None or unit <= 0 else max(0, int((max(ZERO, available) / unit).to_integral_value(rounding=ROUND_FLOOR)))
        scenarios = []
        for fresh in (1000, 500):
            model_cost = D(fresh) * unit if unit is not None else None
            cogs = fixed + h + model_cost if model_cost is not None else None
            scenarios.append({'fresh_paid_requests': fresh, 'remaining_of_1000_without_paid_call': 1000 - fresh,
                              'model_only_cost_usd': model_cost,
                              'model_only_gross_margin_pct_excluding_fixed': D(100) - model_cost if model_cost is not None else None,
                              'full_cogs_usd': cogs, 'gross_margin_pct': D(100) - cogs if cogs is not None else None,
                              'meets_80_pct': cogs <= 20 if cogs is not None else None})
        result['months'][month] = {'fixed_cost_usd': fixed, 'available_model_budget_usd': max(ZERO, available),
                                  'fixed_cost_alone_exceeds_target': available < 0,
                                  'required_unit_cost_for_1000_fresh_usd': max(ZERO, available) / 1000,
                                  'maximum_fresh_paid_calls_at_sample_mean': cap,
                                  'maximum_fresh_of_1000': min(1000, cap) if cap is not None else None,
                                  'minimum_valid_no_paid_call_pct_of_1000': D(1000 - min(1000, cap)) / 10 if cap is not None else None,
                                  'scenarios': scenarios}
    return result


def experiment_rows(directory, prices, audits):
    rows, ignored = [], []
    summary_path = directory / 'execution-summary.json'
    summary = read_json(summary_path) if summary_path.is_file() else None
    for path in sorted(directory.glob('*.json')):
        if path == summary_path:
            continue
        data = read_json(path)
        if isinstance(data, dict) and any(key in data for key in ('validation_receipt', 'source_generation_receipt', 'receipt', 'result_type')):
            rows.append(normalize(path, data, prices, audits))
        else:
            ignored.append(label(path))
    if not rows and not summary:
        raise ValueError('Experiment folder contains no attempt artifacts')
    missing = []
    if summary:
        for case in summary.get('cases', []):
            if case['case'] not in {row['case'] for row in rows}:
                missing.append(case['case'])
                zero = count(case.get('model_calls')) == 0
                rows.append({
                    'case': case['case'], 'artifact': label(directory / (case['case'] + '.json')),
                    'artifact_sha256': None, 'deduplication_key': 'missing:' + str(directory.resolve()) + ':' + case['case'],
                    'invocation_id': None, 'model': None, 'product_result_type': case.get('result_type'),
                    'provider_status': None, 'provider_calls': count(case.get('model_calls')),
                    'actual_cost_usd': ZERO if zero else None, 'cost_status': 'known' if zero else 'unknown',
                    'cost_basis': 'explicit_zero_provider_calls' if zero else 'missing_receipt',
                    'summary_reported_cost_usd': money(case.get('actual_vendor_cost_usd')),
                    'tokens': {key: None for key in ('input_including_cached','input_cached','input_uncached','output_including_reasoning','output_reasoning','output_nonreasoning')},
                    'reported_cost_parts_usd': {'prompt_including_cached': None, 'completion_including_reasoning': None},
                    'derived_token_cost_parts_usd': {key: None for key in ('input_uncached','input_cached','output_nonreasoning','output_reasoning')},
                    'quality': None, 'accounting_issues': ['summary_attempt_artifact_missing'],
                })
    return rows, {'execution_summary': label(summary_path) if summary else None,
                  'execution_summary_sha256': sha(summary_path) if summary else None,
                  'folder_run_complete': summary is not None, 'missing_summary_attempt_artifacts': missing,
                  'summary_model_calls': summary.get('model_calls') if summary else None,
                  'ignored_metadata_files': ignored}


def build_report(baseline_dir, experiments, prices_path, audit_paths=(), h=ZERO):
    prices, audits = catalog_prices(prices_path), quality_index(audit_paths)
    baseline = [normalize(baseline_dir / (case + '.json'), read_json(baseline_dir / (case + '.json')),
                          prices, audits, baseline=True) for case in BASELINE_CASES]
    baseline_by_case = {row['case']: row for row in baseline}
    baseline_stats = aggregate(baseline)
    groups, all_experiment_rows = [], []
    for directory in experiments:
        rows, completeness = experiment_rows(directory, prices, audits)
        summary = aggregate(rows)
        summary_calls = completeness['summary_model_calls']
        if summary_calls is not None and summary_calls != summary['known_provider_calls']:
            raise ValueError('Execution summary call count disagrees with attempt artifacts')
        comparisons = []
        for row in rows:
            original = baseline_by_case.get(row['case'])
            comparisons.append({'case': row['case'], 'experiment_artifact': row['artifact'],
                                'baseline_artifact': original['artifact'] if original else None,
                                'baseline_cost_usd': original['actual_cost_usd'] if original else None,
                                'experiment_cost_usd': row['actual_cost_usd'],
                                **saving(original['actual_cost_usd'] if original else None, row['actual_cost_usd']),
                                'quality': row['quality']})
        matched = [row for row in unique_attempts(rows) if row['case'] in baseline_by_case and row['provider_calls'] != 0]
        comparable = (len(matched) == len(BASELINE_CASES) and {row['case'] for row in matched} == set(BASELINE_CASES)
                      and summary['cost_accounting_complete'] and summary['unknown_call_count_records'] == 0)
        groups.append({'folder': label(directory), 'completeness': completeness, 'attempts': rows,
                       'summary': summary, 'per_case_cost_comparison': comparisons,
                       'same_four_case_set_one_paid_attempt_each': comparable,
                       'mean_paid_call_savings_vs_baseline': saving(baseline_stats['mean_per_paid_call_usd'], summary['mean_per_paid_call_usd']),
                       'four_case_total_savings_vs_baseline': saving(baseline_stats['total_actual_cost_usd'], summary['total_actual_cost_usd']) if comparable else None,
                       'margin_projection': margin_projection(summary['mean_per_paid_call_usd'], h)})
        all_experiment_rows.extend(rows)
    return {
        'kind': 'actual_provider_cost_comparison_with_separate_optional_quality_audit',
        'decimal_encoding': 'Decimal values are JSON strings; arithmetic uses 40 significant digits. Unknown amounts remain null.',
        'no_paid_calls_or_environment_access': True,
        'quality_audit_sources': [{'file': label(path), 'sha256': sha(path)} for path in audit_paths],
        'pricing_catalog': {'file': label(prices_path), 'sha256': sha(prices_path),
                            'use': 'Only to split already-reported prompt costs between cached/uncached tokens when the sum exactly matches. Not a replacement for measured provider cost.'},
        'baseline': {'folder': label(baseline_dir), 'attempts': baseline, 'summary': baseline_stats,
                     'margin_projection': margin_projection(baseline_stats['mean_per_paid_call_usd'], h),
                     'scope': 'Four selected prior real generations, excluding development retries. Final deterministic recompilation did not make new model calls.'},
        'experiments': groups, 'all_experiment_attempts_summary': aggregate(all_experiment_rows),
        'combined_baseline_and_experiment_paid_history': aggregate(baseline + all_experiment_rows),
        'assignment_accounting': {'tier': 'AI-M', 'fixed_unit_cost_usd': D('0.018'),
                                  'different_real_model_changes_assignment_tier_automatically': False,
                                  'model_tier_quality_claim': None,
                                  'margin_projection': margin_projection(D('0.018'), h)},
        'monthly_assumptions': {'subscription_usd': D(100), 'requests': 1000, 'target_gross_margin_pct': D(80),
                                'maximum_cogs_usd': D(20), 'incremental_langfuse_h_usd': h,
                                'h_is_increment_beyond_fixed_infrastructure_only': True,
                                'external_paid_analysis_api_calls': 0,
                                'fixed_cost_status': 'Infrastructure $6 plus collection $3.87 / $4.50 are assignment assumptions, not measured cloud bills.'},
        'limitations': [
            'Quality is null without an explicit artifact-matching audit. Product propose, schema pass, and lower cost are not quality validation.',
            'All unsuccessful and unknown-cost attempts are included. Known subtotals are lower bounds when any cost is unknown; full means and projections then remain null.',
            'Cached input is provider prompt caching and still incurs a paid call. Zero-call exact-result reuse is a separate mechanism.',
            'Paid-call means include failures but do not estimate the cost to obtain a successful action after retries; no retry success distribution has been measured.',
            'Baseline uses four selected successful original generations. Its mean omits prior development failures; development total divided by four is not a steady unit cost.',
            'Small samples, changing prompts/policies/models, and different warm-cache mixtures do not establish production costs, cache hit rates or a quality-preserving reduction.',
            'Per-case comparisons with zero-call results are observed request savings, not evidence that a fresh good recommendation was produced at zero cost.',
            '500 fresh calls require the other 500 requests to be legitimate zero-call outcomes. Quota fallback alone does not maintain recommendation quality.',
            'Reasoning and nonreasoning money are proportional allocations of reported completion cost, not independently billed fields. Cached-input money is allocated only on an exact catalog-price match.',
            'Model-only margin excludes fixed costs. Full margins include the fixed allocation and only additional Langfuse h; existing infrastructure allocation must not be counted twice.',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment_folders', nargs='+', type=Path)
    parser.add_argument('--baseline', type=Path, default=ROOT / 'output/product-v2-final')
    parser.add_argument('--prices', type=Path, default=ROOT / 'docs/cost-model-catalog.json')
    parser.add_argument('--quality-audit', action='append', type=Path, default=[])
    parser.add_argument('--langfuse-monthly-per-customer', type=D, default=ZERO)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/cost-optimization-costs.json')
    args = parser.parse_args()
    h = money(args.langfuse_monthly_per_customer)
    if h is None:
        parser.error('Incremental Langfuse h must be finite and nonnegative')
    with localcontext() as context:
        context.prec = 40
        report = build_report(args.baseline, args.experiment_folders, args.prices, args.quality_audit, h)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=lambda value: format(value, 'f')) + '\n')
    print(json.dumps({'output': str(args.output), 'experiments': len(report['experiments']),
                      'actual_cost_usd': report['all_experiment_attempts_summary']['total_actual_cost_usd'],
                      'unknown_cost_attempts': report['all_experiment_attempts_summary']['unknown_cost_attempts']}, default=str))


if __name__ == '__main__':
    main()
