#!/usr/bin/env python3
"""저장된 영수증으로 비용을 재계산한다. 모델·네트워크 호출은 없다.

금액은 Decimal로 계산하고 JSON에는 문자열로 저장해 부동소수점 변환에 따른
정밀도 손실을 방지한다.
"""
from __future__ import annotations

import argparse
import ast
from decimal import Decimal, ROUND_FLOOR, getcontext
import hashlib
import json
from pathlib import Path
import statistics


ROOT = Path(__file__).resolve().parents[1]
D = Decimal
getcontext().prec = 40
ZERO = D(0)
HUNDRED = D(100)
MILLION = D(1_000_000)


def read_json(path):
    return json.loads(path.read_text(), parse_float=D)


def decimal(value):
    return value if isinstance(value, D) else D(str(value))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def local_path(value):
    path = Path(value)
    if not path.is_absolute():
        path = ROOT / path
    elif not path.is_file() and 'output' in path.parts:
        path = ROOT.joinpath(*path.parts[path.parts.index('output'):])
    return path


def relative(path):
    return str(path.resolve().relative_to(ROOT))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_receipts(report):
    require(report['passed'] and report['unique_invocations'], 'Langfuse reconciliation must pass')
    rows = []
    for case in report['cases']:
        path = local_path(case['file'])
        product = read_json(path)
        receipt = product['validation_receipt']
        usage, raw = receipt['usage'], receipt['raw_usage']
        costs = raw['cost_details']
        actual = decimal(receipt['actual_cost_usd'])
        prompt = decimal(costs['upstream_inference_prompt_cost'])
        completion = decimal(costs['upstream_inference_completions_cost'])
        require(case['passed'] and case['generation_count'] == 1 and receipt['actual_provider_calls'] == 1,
                'Every selected receipt must have exactly one reconciled paid invocation')
        require(receipt['invocation_id'] == case['invocation_id'], 'Receipt identity changed')
        require(actual == raw['cost'] == costs['upstream_inference_cost'] == prompt + completion
                == case['receipt_cost_usd'] == case['observed_cost_details']['total'], 'Cost mismatch')
        input_tokens, output_tokens = usage['input_tokens'], usage['output_tokens']
        cached = usage['input_tokens_details']['cached_tokens']
        reasoning = usage['output_tokens_details']['reasoning_tokens']
        require(0 <= cached <= input_tokens and 0 <= reasoning <= output_tokens, 'Invalid token partition')
        require(input_tokens + output_tokens == usage['total_tokens'], 'Token total mismatch')
        rows.append({
            'artifact': relative(path), 'sha256': digest(path), 'invocation_id': receipt['invocation_id'],
            'model': receipt['model'], 'policy_version': receipt['observation_context']['policy_version'],
            'product_result_type': product['result_type'], 'provider_receipt_status': receipt['status'],
            'final_quality_approved_source': False,
            'tokens': {'input_including_cached': input_tokens, 'input_uncached': input_tokens - cached,
                       'input_cached': cached, 'output_including_reasoning': output_tokens,
                       'output_nonreasoning': output_tokens - reasoning, 'output_reasoning': reasoning},
            'reported_cost_usd': {'total': actual, 'prompt_including_cache': prompt,
                                  'completion_including_reasoning': completion},
        })
    require(len(rows) == report['case_count'], 'Receipt count mismatch')
    require(len({r['invocation_id'] for r in rows}) == len(rows), 'Duplicate paid invocation')
    require(sum((r['reported_cost_usd']['total'] for r in rows), ZERO)
            == report['receipt_total_actual_cost_usd'] == report['langfuse_total_actual_cost_usd'],
            'Reconciliation aggregate mismatch')
    return rows


def link_final_quality(rows, audit):
    by_id = {row['invocation_id']: row for row in rows}
    final = []
    for case in audit['cases']:
        require(case['passed'], 'Final quality audit contains an unpassed case')
        original = case['original_generation']
        row = by_id[original['invocation_id']]
        require(row['artifact'] == original['artifact'] and row['sha256'] == original['artifact_sha256'],
                'Original quality-reviewed generation changed')
        final_path = local_path(case['artifact'])
        require(digest(final_path) == case['artifact_sha256'], 'Final quality-reviewed artifact changed')
        require(case['final_recompilation_model_calls'] == 0, 'Do not silently omit recompilation calls')
        row['final_quality_approved_source'] = True
        final.append({'case': case['case'], 'final_artifact': case['artifact'],
                      'original_artifact': row['artifact'], 'invocation_id': row['invocation_id'],
                      'original_generation_policy': row['policy_version'], 'quality_score': case['total'],
                      'original_generation_cost_usd': row['reported_cost_usd']['total'],
                      'final_recompilation_model_calls': 0, 'validation_mode': case['validation_mode']})
    require(len({case['invocation_id'] for case in final}) == len(final), 'Final cases reuse one invocation')
    return final


def allocate_token_costs(rows):
    cold_rates = {row['reported_cost_usd']['prompt_including_cache'] / row['tokens']['input_uncached']
                  for row in rows if not row['tokens']['input_cached']}
    output_rates = {row['reported_cost_usd']['completion_including_reasoning']
                    / row['tokens']['output_including_reasoning'] for row in rows}
    require(len(cold_rates) == len(output_rates) == 1, 'Cannot infer one consistent observed rate')
    cold_rate, output_rate = cold_rates.pop(), output_rates.pop()
    cache_rates = {(row['reported_cost_usd']['prompt_including_cache']
                    - row['tokens']['input_uncached'] * cold_rate) / row['tokens']['input_cached']
                   for row in rows if row['tokens']['input_cached']}
    require(len(cache_rates) == 1, 'Cannot infer one consistent observed cache rate')
    cache_rate = cache_rates.pop()
    for row in rows:
        token = row['tokens']
        allocated = {'input_uncached': token['input_uncached'] * cold_rate,
                     'input_cached': token['input_cached'] * cache_rate,
                     'output_nonreasoning': token['output_nonreasoning'] * output_rate,
                     'output_reasoning': token['output_reasoning'] * output_rate}
        require(sum(allocated.values(), ZERO) == row['reported_cost_usd']['total'], 'Allocation mismatch')
        row['derived_partition_cost_usd'] = allocated
    return {'basis': 'Inferred exactly from these provider-reported prompt/completion costs and token counts; not a future tariff promise.',
            'uncached_input_per_million_usd': cold_rate * MILLION,
            'cached_input_per_million_usd': cache_rate * MILLION,
            'output_including_reasoning_per_million_usd': output_rate * MILLION,
            'partition_caveat': 'Cached-input and reasoning money are derived allocations. The provider directly reports only prompt, completion, and total money; reasoning is included in completion, not an extra fee.'}


def aggregate(rows):
    costs = [row['reported_cost_usd']['total'] for row in rows]
    require(bool(costs), 'An accounting cohort cannot be empty')
    count = D(len(rows))
    reported = {key: sum((row['reported_cost_usd'][key] for row in rows), ZERO)
                for key in rows[0]['reported_cost_usd']}
    partitions = {key: sum((row['derived_partition_cost_usd'][key] for row in rows), ZERO)
                  for key in rows[0]['derived_partition_cost_usd']}
    tokens = {key: sum(row['tokens'][key] for row in rows) for key in rows[0]['tokens']}
    return {'count': len(rows), 'total_cost_usd': sum(costs, ZERO),
            'mean_cost_usd': sum(costs, ZERO) / count, 'median_cost_usd': statistics.median(costs),
            'min_cost_usd': min(costs), 'max_cost_usd': max(costs),
            'reported_cost_totals_usd': reported,
            'derived_partition_totals_usd': partitions,
            'derived_partition_means_usd': {key: value / count for key, value in partitions.items()},
            'token_totals': tokens, 'mean_tokens': {key: D(value) / count for key, value in tokens.items()},
            'provider_prompt_cache_used_calls': sum(row['tokens']['input_cached'] > 0 for row in rows)}


def economics(unit, base, h, subscription, requests, target_cogs):
    available = target_cogs - base - h
    paid_limit = min(requests, max(0, int((available / unit).to_integral_value(rounding=ROUND_FLOOR))))
    continuous_avoidance = max(ZERO, min(D(1), D(1) - available / (D(requests) * unit)))
    scenarios = []
    for paid in (1000, 500, 100, 50):
        cogs = base + h + D(paid) * unit
        scenarios.append({'paid_calls': paid, 'no_paid_call_requests': requests - paid,
                          'assumed_exact_result_cache_pct_if_only_cache': D(requests - paid) / D(requests) * HUNDRED,
                          'llm_cost_usd': D(paid) * unit, 'cogs_usd': cogs,
                          'gross_margin_pct': (subscription - cogs) / subscription * HUNDRED,
                          'meets_80_pct': cogs <= target_cogs,
                          'maximum_additional_langfuse_h_usd': target_cogs - base - D(paid) * unit})
    return {'assumed_per_paid_call_usd': unit, 'fixed_cost_excluding_incremental_langfuse_usd': base,
            'incremental_langfuse_h_usd': h, 'available_llm_budget_usd': max(ZERO, available),
            'fixed_cost_alone_exceeds_target': available < 0,
            'required_unit_cost_for_all_requests_paid_usd': max(ZERO, available) / D(requests),
            'required_unit_cost_reduction_pct': max(ZERO, (D(1) - max(ZERO, available) / D(requests) / unit) * HUNDRED),
            'max_paid_calls_at_assumed_unit_cost': paid_limit,
            'required_no_paid_call_requests_integer': requests - paid_limit,
            'required_exact_result_cache_pct_integer_if_only_cache': D(requests - paid_limit) / D(requests) * HUNDRED,
            'required_exact_result_cache_pct_continuous_if_only_cache': continuous_avoidance * HUNDRED,
            'scenarios': scenarios}


def ledger_snapshot():
    path = ROOT / 'src/vivi_agent/ledger.py'
    source = path.read_text()
    cls = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == 'Ledger')
    init = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '__init__')
    defaults = {arg.arg: ast.literal_eval(value) for arg, value in zip(init.args.args[-len(init.args.defaults):], init.args.defaults)}
    complete = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'complete')
    complete_source = ast.get_source_segment(source, complete)
    require('cost_micros' not in complete_source, 'Ledger completion behavior changed: revisit the budget analysis')
    return {'source': relative(path), 'source_sha256': digest(path),
            'unit_cost_usd': decimal(defaults['unit_cost']), 'monthly_call_cap': defaults['monthly_call_cap'],
            'fixed_monthly_cost_usd': decimal(defaults['fixed_monthly_cost']),
            'maximum_monthly_cogs_usd': decimal(defaults['max_monthly_cogs']),
            'actual_provider_cost_budget_enforced': False,
            'evidence': 'Reservations and remaining budget use fixed cost_micros. complete() saves provider_receipt_json without replacing cost_micros with actual cost. stats() sums cost_micros.',
            'conclusion': 'The default $0.018 / 500-call cap constrains fictional assignment spend, not real provider spend or delivery quality for the remaining requests.'}


def build_report(report_path, audit_path, prices_path, h):
    reconciliation, audit, prices = (read_json(path) for path in (report_path, audit_path, prices_path))
    rows = load_receipts(reconciliation)
    final_links = link_final_quality(rows, audit)
    rates = allocate_token_costs(rows)
    selected = [row for row in rows if row['final_quality_approved_source']]
    cohorts = {
        'all_development_paid_calls': aggregate(rows),
        'final_quality_approved_original_generations': aggregate(selected),
        'product_fallback_calls': aggregate([row for row in rows if row['product_result_type'] == 'fallback']),
        'provider_failed_calls': aggregate([row for row in rows if row['provider_receipt_status'] == 'failed']),
        'nonfinal_development_calls': aggregate([row for row in rows if not row['final_quality_approved_source']]),
        'provider_prompt_cache_warm_calls': aggregate([row for row in rows if row['tokens']['input_cached'] > 0]),
        'provider_prompt_cache_cold_calls': aggregate([row for row in rows if row['tokens']['input_cached'] == 0]),
    }
    subscription = decimal(prices['customer_monthly_subscription_usd'])
    requests = prices['monthly_requests_per_customer']
    require(requests == 1000, 'Scenario request counts must be revisited for a different monthly request assumption')
    target = decimal(prices['maximum_cogs_per_customer_usd'])
    units = {item['cost_id']: decimal(item['unit_cost_usd']) for item in prices['cost_items']}
    collection_avg = D(225) * decimal(prices['collection']['weeks_per_month']) * units['COL001']
    bases = {'average_4_3_week_month': units['INF001'] + collection_avg,
             'five_collection_month': units['INF001'] + D(225 * 5) * units['COL001']}
    all_calls, finals = cohorts['all_development_paid_calls'], cohorts['final_quality_approved_original_generations']
    baselines = {'all_10_mean': all_calls['mean_cost_usd'], 'all_10_median': all_calls['median_cost_usd'],
                 'final_4_original_mean': finals['mean_cost_usd']}
    real_economics = {label: {month: economics(unit, base, h, subscription, requests, target)
                            for month, base in bases.items()} for label, unit in baselines.items()}
    assignment = {month: economics(units['AI-M'], base, h, subscription, requests, target)
                  for month, base in bases.items()}
    output_mean = finals['reported_cost_totals_usd']['completion_including_reasoning'] / D(len(selected))
    ledger = ledger_snapshot()
    ledger['fictional_cogs_at_cap_usd'] = ledger['fixed_monthly_cost_usd'] + D(ledger['monthly_call_cap']) * ledger['unit_cost_usd']
    return {
        'kind': 'saved_real_provider_receipt_economics_separate_from_assignment_tariffs',
        'decimal_encoding': 'Exact finite monetary amounts and Decimal ratios are JSON strings; division uses 40-digit Decimal precision. Token counts are integers.',
        'reproduce': 'python3 scripts/analyze_actual_costs.py --langfuse-monthly-per-customer 0',
        'sources': [{'file': relative(path), 'sha256': digest(path)} for path in (report_path, audit_path, prices_path)],
        'scope': {'paid_calls': len(rows), 'final_quality_approved_cases': len(final_links),
                  'all_selected_receipts_reconciled_with_langfuse': True,
                  'additional_model_calls_in_this_analysis': 0, 'reads_environment_or_credentials': False,
                  'all_failed_paid_attempts_included': True, 'unrelated_test_traces_excluded': True},
        'observed_provider_rates': rates, 'calls': rows, 'final_quality_case_sources': final_links,
        'cohorts': cohorts,
        'development_spend_per_selected_final_artifact_usd': all_calls['total_cost_usd'] / D(len(final_links)),
        'development_spend_divided_by_four_is_steady_unit_cost': False,
        'monthly_assumptions': {
            'subscription_usd': subscription, 'requests': requests, 'target_gross_margin_pct': prices['target_gross_margin_pct'],
            'maximum_cogs_usd': target, 'infrastructure_allocation_usd': units['INF001'],
            'average_collection_usd': collection_avg, 'five_collection_month_usd': D(225 * 5) * units['COL001'],
            'incremental_langfuse_h_usd': h,
            'h_meaning': 'Only incremental per-customer monthly Langfuse cost beyond INF001. If Langfuse is already inside the $6 infrastructure allocation, h=0; do not double-count it. No measured monthly dollar amount is available.',
            'fixed_cost_status': 'Infrastructure and collection remain assignment assumptions; these are not measured cloud or collection bills.',
            'external_analysis_api_calls_assumed': 0,
            'external_api_caveat': 'Local analysis is assumed. Add actual external API costs (assignment API001 per executed call) if a paid API is introduced.',
            'formulas': {'cogs': 'fixed + h + paid_calls * observed_unit_cost',
                         'margin_pct': '(100 - cogs) / 100 * 100',
                         'unit_target_average_month': '0.01013 - h / 1000',
                         'unit_target_five_collection_month': '0.0095 - h / 1000',
                         'paid_cap': 'min(1000, max(0, floor((20 - fixed - h) / unit_cost)))',
                         'exact_cache_pct_if_only_cache': '(1000 - paid_cap) / 1000 * 100'},
            'no_paid_call_quality_condition': 'Exact-result reuse must match unchanged evidence, product context, policy and task scope. Genuine rule-based no_action/insufficient outcomes can also avoid calls; quota fallback is not equivalent to a good recommendation. Required cache percentages are thresholds, not measured hit rates.'},
        'actual_provider_cost_sensitivity': real_economics,
        'fictional_assignment_ai_m': {
            'unit_cost_usd': units['AI-M'], 'unit_cost_rules': prices['unit_cost_rules'],
            'token_optimization_changes_fictional_tier_price': False,
            'cheaper_real_model_automatically_becomes_ai_s': False,
            'quality_tradeoff_from_assignment': [task for task in prices['model_tier_quality']['tasks']
                                               if task['task_id'] in ('diagnose', 'draft_action')],
            'months': assignment},
        'legacy_ledger_snapshot': ledger,
        'optimization_arithmetic_bounds_not_quality_validated': {
            'cohort': 'final_quality_approved_original_generations',
            'even_zero_prompt_cost_remaining_output_cost_per_call_usd': output_mean,
            'even_zero_reasoning_cost_remaining_cost_per_call_usd': finals['mean_cost_usd'] - finals['derived_partition_means_usd']['output_reasoning'],
            'maximum_output_tokens_if_input_were_free_for_1000_paid_calls': {
                month: max(ZERO, (target - base - h) / D(requests)) / (rates['output_including_reasoning_per_million_usd'] / MILLION)
                for month, base in bases.items()},
            'warning': 'These are mathematical lower bounds, not executable quality-preserving changes. Removing reasoning or shrinking output may damage the validated action-selection contract. Prompt cache alone cannot remove paid output costs.'},
        'limitations': [
            '10 development invocations are a small, selected sample across policy versions, not a production workload or future price ceiling.',
            'Five calls used warm provider prompt caches from repeated development data. Their input discounts and case mix do not establish future cache hit rates or cold-request costs.',
            'Provider prompt cache discounts input tokens but still invokes and bills the model. Application exact-result cache avoids a call; the two rates must not be conflated.',
            'The final four artifacts passed independent model-based review on provided synthetic data; they are not external human adoption or observed business-effect validation.',
            'Final artifacts were deterministic recompilations with zero additional calls. Their underlying original calls span v2.1/v2.2/v2.3; only security was freshly generated with v2.3.',
            'Product fallback, provider failure, and not-selected final output are overlapping cohorts. Do not add their costs together; all 10 are counted exactly once in the development total.',
            'Development total divided by four is historical spend per selected artifact, not steady per-request cost; final-four average also excludes the cost of future failures/retries.',
            'Reported usage.cost is measured provider inference cost, not a complete invoice including any account funding, tax, exchange-rate or other fees.',
            'A call-count cap or rejected quota fallback does not establish recommendation quality for every one of the 1000 requests.',
            'Langfuse instantaneous memory measurement (~3.03 GiB) is not a measured monthly dollar bill or per-customer allocation.',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipts-report', type=Path, default=ROOT / 'output/product-v2-langfuse.json')
    parser.add_argument('--quality-audit', type=Path, default=ROOT / 'output/product-v2-quality-audit.json')
    parser.add_argument('--prices', type=Path, default=ROOT / 'VIVI_MVP_Sample_Dataset/pricing_assumptions.json')
    parser.add_argument('--langfuse-monthly-per-customer', type=D, default=ZERO,
                        help='Incremental USD beyond INF001 only; use 0 if already included. No observed bill is assumed.')
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/actual-cost-analysis.json')
    args = parser.parse_args()
    if not args.langfuse_monthly_per_customer.is_finite() or args.langfuse_monthly_per_customer < 0:
        parser.error('Incremental Langfuse allocation must be a finite nonnegative Decimal')
    report = build_report(args.receipts_report, args.quality_audit, args.prices, args.langfuse_monthly_per_customer)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=lambda value: format(value, 'f')) + '\n')
    totals = report['cohorts']
    print(json.dumps({'output': str(args.output), 'paid_calls': report['scope']['paid_calls'],
                      'development_total_usd': totals['all_development_paid_calls']['total_cost_usd'],
                      'all_call_mean_usd': totals['all_development_paid_calls']['mean_cost_usd'],
                      'all_call_median_usd': totals['all_development_paid_calls']['median_cost_usd'],
                      'final_four_original_mean_usd': totals['final_quality_approved_original_generations']['mean_cost_usd']},
                     default=str))


if __name__ == '__main__':
    main()
