"""로컬 근거와 정책을 적용하고 유료 초안 호출을 최대 한 번 수행하는 운영용 LangGraph."""
from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from .analytics import DuckDBAnalytics, priorities
from .graph import digest
from .ledger import BudgetExceeded, InFlightError, Ledger
from .actual_budget import ActualBudget, ActualBudgetExceeded, ActualBudgetInFlight, ActualPriceUnknown
from .compact import CompactDraft, expand_draft, build_generation_input
from .measurement import compile_measurement, render_measurement
from .selection import (ProductPlan, base_plan, validate_selection, render_problem_summary,
                        retain_selected_prerequisites, align_visibility_outcomes)
from .execution_policy import compile_execution_policy, render_execution_policy
from .validation import validate_plan

VERSION = 'product-one-call.v3.0'


class ProductState(TypedDict, total=False):
    pack: dict
    priority: list
    existing_actions: list
    selected_question_id: str
    key: str
    plan: dict | None
    execution_contracts: list
    result_type: str
    reason: str
    errors: list
    events: list
    model_calls: int
    cache_hit: bool
    accounting_error: str


class ProductAgent:
    def __init__(self, repository, provider, state_dir, action_store):
        self.repository, self.provider = repository, provider
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.action_store = action_store
        self.analytics = DuckDBAnalytics(repository, self.state_dir)

    def get_topic(self, topic_id, snapshot_id='SNAP-2'):
        pack = self.analytics.build(topic_id, snapshot_id)
        return {'evidence': pack, 'priority': priorities(pack)}

    def list_topics(self):
        questions = json.loads((self.repository.dataset_dir / 'questions.json').read_text())
        topics = {q['topic_id']: q['topic_label'] for q in questions}
        items = []
        for topic_id, name in topics.items():
            detail = self.get_topic(topic_id)
            pack, priority = detail['evidence'], detail['priority']
            def rate(mid):
                m = pack['metrics'][mid]
                return m['numerator'] / m['denominator'] if m['denominator'] else None
            items.append({'topic_id': topic_id, 'name': name, 'coverage': pack['quality']['coverage'],
                          'mention_rate': rate('topic.target_mentions'),
                          'recommendation_rate': rate('topic.target_recommendations'),
                          'priority_score': priority[0]['score'], 'priority_reason': priority[0]['reason']})
        return {'items': sorted(items, key=lambda x: (-x['priority_score'], x['topic_id'])),
                'dataset_version': self.repository.dataset_hash}

    def _validated(self, value, pack, question_id):
        if isinstance(value, dict) and value.get('kind') == 'compact_action_draft.v1':
            value = expand_draft(value, pack, question_id)
        plan = retain_selected_prerequisites(ProductPlan.model_validate(value).model_dump())
        errors = validate_plan(base_plan(plan), pack) + validate_selection(plan, pack, question_id)
        if plan['decision'] != 'recommend' or len(plan['actions']) != 1:
            errors.append('This eligible opportunity requires one focused draft')
        contracts = []
        for index, action in enumerate(plan['actions']):
            if action['question_ids'] != [question_id]:
                errors.append('Draft changed the selected question scope')
            try:
                outcome_policy = align_visibility_outcomes(action, question_id) if not errors else None
                contract = compile_measurement(action, pack)
                if outcome_policy:
                    contract['outcome_policy'] = outcome_policy
                action['measurement'].update(render_measurement(contract))
                contracts.append({'action_index': index, **contract})
            except ValueError as exc:
                errors.append(str(exc))
        if not errors:
            try:
                action = plan['actions'][0]
                policy = compile_execution_policy(action, pack)
                plan['actions'][0] = render_execution_policy(action, policy)
                contracts[0]['execution_policy'] = policy
                if action['target_asset']['mode'] == 'operational':
                    contracts[0]['status'] = 'context_baseline_only'
                    plan['actions'][0]['measurement'].update({
                        'follow_up': '현재 선정한 내부 작업의 완료 기준과 검증 기록을 확인합니다. 게시를 전제로 한 노출 수집은 이 작업에서 시작하지 않습니다.',
                        'positive_signal': '내부 작업의 acceptance_criteria 충족 여부만 확인합니다. 현재 노출 수치는 참고 기준선이며 작업 효과를 입증하지 않습니다.',
                        'stop_or_revise': '필수 검증을 통과하지 못하면 작업을 보류하거나 후보 선정을 재검토합니다. 이후 게시 작업이 별도로 선정되면 그때 측정 범위와 일정을 확정합니다.',
                    })
                plan = render_problem_summary(plan, pack, question_id)
            except ValueError as exc:
                errors.append(str(exc))
        if not errors:
            action = plan['actions'][0]
            plan['monitoring_plan'] = '\n'.join(action['measurement'][field] for field in
                                               ('follow_up', 'positive_signal', 'stop_or_revise'))
        return plan, contracts, errors

    def run(self, customer_id, topic_id, snapshot_id='SNAP-2', context=None):
        if not isinstance(customer_id, str) or not customer_id.strip() or len(customer_id) > 128:
            raise ValueError('Invalid customer scope')
        tenant_dir = self.state_dir / 'customers' / digest(customer_id)
        tenant_dir.mkdir(parents=True, exist_ok=True)
        ledger = Ledger(tenant_dir / 'usage.sqlite', max_attempts=1, monthly_call_cap=500,
                        fixed_monthly_cost=10.5)
        actual_budget = ActualBudget(tenant_dir / 'actual-usage.sqlite')
        actual_budget.import_legacy(ledger.path)
        # 월 5회 수집비 $4.50와 인프라 $6을 확보한다.
        # 월평균 추정치 $9.87보다 여유를 두며 계정 전체 예산은 초기화하지 않는다.
        lock_name = digest({'topic': topic_id, 'snapshot': snapshot_id, 'context': context})
        with (tenant_dir / f'{lock_name}.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._run_graph(customer_id, topic_id, snapshot_id, context, tenant_dir, ledger, actual_budget)

    def _run_graph(self, customer_id, topic_id, snapshot_id, context, tenant_dir, ledger, actual_budget):
        def event(state, name): return state.get('events', []) + [name]
        def load(state):
            pack = self.analytics.build(topic_id, snapshot_id, context)
            # 관계 조회 모델도 같은 데이터셋 해시를 사용하며 JSON에서 다시 만들 수 있다.
            from .relations import RelationProjection
            relations = RelationProjection(self.repository, self.state_dir).paths(pack)
            if relations['dataset_version'] != pack['dataset_version']:
                raise ValueError('Relation/analytics version mismatch')
            pack['relations'] = relations
            return {'pack': pack, 'priority': priorities(pack), 'events': event(state, 'load_evidence')}

        def policy(state):
            pack = state['pack']
            existing = [{key: row.get(key) for key in ('id', 'topic_id', 'question_ids', 'title', 'notes', 'status', 'dataset_version')}
                        for row in self.action_store.list_actions(customer_id, topic_id=topic_id)]
            update = {'existing_actions': existing, 'events': event(state, 'policy_and_current_actions')}
            if pack['quality']['coverage'] < .8:
                return update | {'result_type': 'insufficient_evidence',
                                 'reason': '유효 수집률이 80% 미만입니다. 누락된 질문·채널부터 재수집해야 합니다.'}
            metric = pack['metrics']['topic.target_recommendations']
            if metric['denominator'] and metric['numerator'] == metric['denominator']:
                return update | {'result_type': 'no_action', 'reason': '모든 유효 응답에서 추천되고 있어 유지 관찰합니다.'}
            blocked = set()
            for action in existing:
                if action['status'] in ('draft', 'in_progress') or (
                    action['status'] == 'done' and action.get('dataset_version') == pack['dataset_version']
                ):
                    blocked.update(action.get('question_ids', []))
            candidates = [row for row in state['priority'] if row['score'] > 0 and row['question_id'] not in blocked]
            if not candidates:
                return update | {'result_type': 'no_action', 'reason': '현재 근거에서 새로 실행할 과업이 없거나 기존 액션과 중복됩니다.'}
            selected = candidates[0]['question_id']
            code = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                    for name in ('product.py', 'analytics.py', 'relations.py', 'validation.py', 'models.py',
                                 'measurement.py', 'integration.py', 'prompts.py', 'graph.py', 'evidence.py', 'provider.py',
                                 'selection.py', 'compact.py', 'actual_budget.py', 'execution_policy.py', 'observability.py')}
            key = digest({'customer': customer_id, 'scope': pack['scope'], 'dataset': pack['dataset_version'],
                          'context': pack['context_hash'], 'selected': selected, 'existing': existing,
                          'policy': VERSION, 'code': code, 'schema': CompactDraft.model_json_schema(),
                          'model': getattr(self.provider, 'cache_identity', 'unconfigured')})
            return update | {'selected_question_id': selected, 'key': key}

        def cache(state):
            path = tenant_dir / 'cache' / f"{state['key']}.json"
            update = {'events': event(state, 'exact_cache')}
            if path.exists():
                try:
                    saved = json.loads(path.read_text())
                    plan, contracts, errors = self._validated(saved['raw_plan'], state['pack'], state['selected_question_id'])
                    if not errors and saved['dataset_version'] == state['pack']['dataset_version']:
                        return update | {'result_type': 'cache_hit', 'plan': plan, 'execution_contracts': contracts,
                                         'cache_hit': True, 'reason': '같은 데이터·맥락·정책의 이전 초안을 재사용했습니다. 현재 액션 상태도 재확인했습니다.'}
                except (ValueError, KeyError, TypeError):
                    pass
            return update

        def generate(state):
            update = {'events': event(state, 'generate_once')}
            if self.provider is None:
                return update | {'result_type': 'fallback', 'reason': '모델 키가 설정되지 않았습니다. 근거와 규칙 기반 우선순위만 제공합니다.'}
            prompt, schema = build_generation_input(state['pack'], state['selected_question_id'],
                                                    state['existing_actions'], state['priority'])
            prompt_hash = digest({'prompt': prompt, 'schema': schema})
            previous = ledger.get_completed(state['key'], 'design', prompt_hash)
            if previous is not None:
                return update | {'plan': previous}
            try:
                estimator = getattr(self.provider, 'estimated_max_cost', None)
                if not callable(estimator):
                    raise ActualPriceUnknown('Provider has no verified cost estimator')
                quote = estimator(prompt, schema, stage='design')
                actual_reservation = actual_budget.reserve(state['key'], quote)
            except (ActualBudgetExceeded, ActualBudgetInFlight, ActualPriceUnknown) as exc:
                return update | {'result_type': 'fallback', 'errors': [type(exc).__name__],
                                 'reason': '실제 비용 예산 또는 미확인 과금 때문에 새 생성을 보류합니다. 추가 모델 호출은 하지 않았습니다.'}
            try:
                receipt = ledger.reserve(state['key'], 'design', prompt_hash)
            except (BudgetExceeded, InFlightError):
                actual_budget.release(actual_reservation)
                return update | {'result_type': 'fallback', 'reason': '호출 예산 또는 이전 시도 제한으로 새 생성을 보류합니다. 근거와 우선순위를 확인하세요.'}
            try:
                actual_budget.bind_legacy(actual_reservation, receipt)
                actual_budget.mark_started(actual_reservation)
            except Exception as exc:
                # 아직 공급자 요청 전이다. 시작하지 않았음이 확인된 예약만 해제하고
                # 회계 상태가 불확실하면 예약을 유지한다.
                try:
                    ledger.cancel_before_provider_call(receipt, type(exc).__name__)
                    actual_budget.release(actual_reservation)
                except Exception:
                    pass
                return update | {'result_type': 'fallback', 'model_calls': 0,
                                 'accounting_error': type(exc).__name__,
                                 'reason': '호출 전 비용 예약을 확정하지 못해 생성을 보류했습니다. 추가 모델 호출은 하지 않았습니다.'}
            try:
                self.provider.observation_context = {
                    'topic_id': topic_id, 'snapshot_id': snapshot_id,
                    'question_id': state['selected_question_id'], 'policy_version': VERSION,
                    'dataset_version': state['pack']['dataset_version'], 'run_key_hash': state['key'],
                }
                value = self.provider.generate('design', prompt, schema)
                ledger.complete(receipt, value, self.provider.last_receipt)
                outcome = {'plan': value, 'model_calls': 1}
            except Exception as exc:
                ledger.fail(receipt, type(exc).__name__, getattr(self.provider, 'last_receipt', {}))
                outcome = {'result_type': 'fallback', 'reason': '모델 생성에 실패했습니다. 자동 재호출 없이 근거 요약을 제공합니다.',
                           'model_calls': 1, 'errors': [type(exc).__name__]}
            finally:
                try:
                    actual_budget.settle(actual_reservation, getattr(self.provider, 'last_receipt', {}))
                except Exception as exc:
                    # 정산 실패 시 예약을 유지하고 유료 호출은 반복하지 않는다.
                    update['accounting_error'] = type(exc).__name__
            return update | outcome

        def validate(state):
            update = {'events': event(state, 'validate_and_compile')}
            try:
                plan, contracts, errors = self._validated(state['plan'], state['pack'], state['selected_question_id'])
            except (ValidationError, ValueError, TypeError, KeyError) as exc:
                errors, plan, contracts = [type(exc).__name__], None, []
            if errors:
                return update | {'result_type': 'fallback', 'plan': None, 'errors': errors,
                                 'reason': '초안이 구조·근거 검사를 통과하지 못했습니다. 추가 호출 없이 생성을 보류합니다.'}
            path = tenant_dir / 'cache' / f"{state['key']}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix('.tmp')
            temp.write_text(json.dumps({'raw_plan': state['plan'], 'dataset_version': state['pack']['dataset_version']}, ensure_ascii=False))
            temp.replace(path)
            return update | {'result_type': 'propose', 'plan': plan, 'execution_contracts': contracts,
                             'reason': '근거·측정 계약 검사를 통과한 초안입니다. 제품 사실과 실행 가능성을 검토한 뒤 저장하세요.'}

        graph = StateGraph(ProductState)
        for name, fn in [('load', load), ('policy', policy), ('cache', cache), ('generate', generate), ('validate', validate)]:
            graph.add_node(name, fn)
        graph.add_edge(START, 'load'); graph.add_edge('load', 'policy')
        graph.add_conditional_edges('policy', lambda s: END if s.get('result_type') else 'cache')
        graph.add_conditional_edges('cache', lambda s: END if s.get('result_type') else 'generate')
        graph.add_conditional_edges('generate', lambda s: END if s.get('result_type') else 'validate')
        graph.add_edge('validate', END)
        state = graph.compile().invoke({'events': [], 'model_calls': 0, 'cache_hit': False, 'plan': None})
        pack = state['pack']
        result = {'result_type': state['result_type'], 'scope': pack['scope'], 'topic_id': topic_id,
                  'dataset_version': pack['dataset_version'], 'policy_version': VERSION,
                  'plan': state.get('plan'), 'execution_contracts': state.get('execution_contracts', []),
                  'priority': state['priority'], 'reason': state['reason'], 'requires_human_review': True,
                  'errors': state.get('errors', []), 'events': state['events'],
                  'evidence_summary': {'quality': pack['quality'], 'metrics': pack['metrics'],
                                       'excluded_pairs': [{'question_id': r['question_id'], 'channel': r['channel'], 'reason': r['reason']}
                                                          for r in pack['excluded'] if r['reason'] != 'superseded'],
                                       'relations': pack['relations']},
                  'usage': {'model_calls': state['model_calls'], 'external_api_calls': 0,
                            'assignment_cost_usd': round(state['model_calls'] * .018, 6),
                            'cache_hit': state['cache_hit'],
                            'actual_vendor_cost_usd': getattr(self.provider, 'last_receipt', {}).get('actual_cost_usd') if state['model_calls'] else 0,
                            'actual_budget': actual_budget.stats(),
                            'accounting_error': state.get('accounting_error'),
                            'ledger': ledger.stats(state.get('key', lockless_key(customer_id, topic_id)))}}
        self.action_store.record_event(customer_id, 'agent_run', result)
        return result


def lockless_key(customer_id, topic_id):
    return digest({'customer': customer_id, 'topic': topic_id, 'kind': 'no_paid_run'})
