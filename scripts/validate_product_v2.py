"""각 승인 검증 사례를 1회 호출하고 실행마다 새 폴더에 보존한다.

일치하는 캐시가 없으면 설정된 유료 모델을 호출한다.
생성 실패를 재시도하거나 고객의 공유 예산 원장을 초기화하지 않는다.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path

from dotenv import load_dotenv

from vivi_agent.evidence import EvidenceRepository
from vivi_agent.product import ProductAgent, VERSION
from vivi_agent.provider import create_provider
from vivi_agent.store import ActionStore

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    'lead': ('lead', None),
    'unsupported': ('lead', 'lead_capability_unsupported.json'),
    'security': ('security', None),
    'confirmed': ('lead', 'lead_capability_confirmed.json'),
    'automation': ('automation', None),
    'scale': ('scale', None),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', nargs='+', choices=CASES, default=['lead', 'unsupported', 'security'])
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--customer', default='demo')
    parser.add_argument('--workers', type=int, default=3, choices=range(1, 4))
    parser.add_argument('--model', help='Explicit model profile for this independent experiment')
    parser.add_argument('--reasoning-effort', choices=['none', 'low', 'medium', 'high', 'xhigh'])
    parser.add_argument('--max-output-tokens', type=int)
    args = parser.parse_args()
    # 실패한 첫 시도도 보존하도록 덮어쓰기를 금지한다.
    args.output_dir.mkdir(parents=True, exist_ok=False)
    load_dotenv(ROOT / '.env', override=False)
    state = ROOT / '.vivi' / 'product'

    def run(case):
        topic, context_file = CASES[case]
        context = json.loads((ROOT / 'evals/contexts' / context_file).read_text()) if context_file else None
        overrides = {key: value for key, value in {'model': args.model,
                     'reasoning_effort': args.reasoning_effort,
                     'max_output_tokens': args.max_output_tokens}.items() if value is not None}
        provider = create_provider(artifact_dir=ROOT / '.vivi/traces', **overrides)
        runner = ProductAgent(EvidenceRepository(ROOT / 'VIVI_MVP_Sample_Dataset'), provider,
                              state, ActionStore(state / 'actions.sqlite'))
        result = runner.run(args.customer, topic, context=context)
        if result['usage']['model_calls']:
            result['validation_receipt'] = provider.last_receipt
        (args.output_dir / f'{case}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        row = {'case': case, 'result_type': result['result_type'], **result['usage'], 'errors': result['errors']}
        print(json.dumps({key: row[key] for key in ('case', 'result_type', 'model_calls', 'actual_vendor_cost_usd', 'errors')}, ensure_ascii=False), flush=True)
        return row

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(run, args.cases))
    costs = [row['actual_vendor_cost_usd'] for row in rows]
    summary = {
        'recorded_at': datetime.now(timezone.utc).isoformat(), 'policy_version': VERSION,
        'kind': 'provided_synthetic_dataset_real_model_not_effectiveness_validation',
        'customer': args.customer, 'cases': rows,
        'model_calls': sum(row['model_calls'] for row in rows),
        'known_actual_vendor_cost_usd': sum(cost for cost in costs if cost is not None),
        'unknown_cost_cases': [row['case'] for row in rows if row['actual_vendor_cost_usd'] is None],
    }
    (args.output_dir / 'execution-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
