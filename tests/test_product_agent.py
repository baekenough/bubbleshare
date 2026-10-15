"""통제된 유료 호출 대역으로 제품 계약을 검증한다. 모델 품질 평가는 아니다."""
from copy import deepcopy
import json
import hashlib
import uuid
from pathlib import Path

import pytest

from vivi_agent.evidence import EvidenceRepository
from vivi_agent.product import ProductAgent

ROOT = Path(__file__).resolve().parents[1]


class Store:
    def __init__(self): self.actions, self.events = [], []
    def list_actions(self, customer_id, topic_id=None):
        return [deepcopy(a) for a in self.actions if a['customer_id'] == customer_id and a['topic_id'] == topic_id]
    def record_event(self, customer_id, event_type, payload):
        self.events.append((customer_id, event_type, deepcopy(payload)))


class Provider:
    cache_identity = 'test-product-model'
    model = 'openai/gpt-5.4'
    last_receipt = {'actual_provider_calls': 0, 'test_double': True}
    def __init__(self, invalid=False, fail=False): self.calls, self.invalid, self.fail = 0, invalid, fail
    def estimated_max_cost(self, prompt, schema, stage='design'):
        from vivi_agent.actual_budget import PRICE_CATALOG_ID
        return {'estimated_max_cost_usd': '0.02', 'price_catalog_id': PRICE_CATALOG_ID, 'pricing_known': True,
                'provider': 'openrouter', 'requested_model': self.model,
                'prompt_hash': hashlib.sha256(prompt.encode()).hexdigest()}
    def generate(self, stage, prompt, schema):
        self.calls += 1
        self.last_receipt = {'actual_provider_calls': 1, 'test_double': True, 'actual_cost_usd': .01,
                             'prompt_hash': hashlib.sha256(prompt.encode()).hexdigest(),
                             'requested_model': self.model, 'invocation_id': uuid.uuid4().hex,
                             'status': 'failed' if self.fail else 'completed'}
        if self.fail: raise RuntimeError('simulated transport failure')
        if self.invalid: return {'unexpected': 'schema failure'}
        payload, _ = json.JSONDecoder().raw_decode(prompt.split('\nINPUT JSON:\n', 1)[1])
        plan = json.loads((ROOT / 'output/lead.v8.json').read_text())['plan']
        qid = payload['selected_question_id']
        action = plan['actions'][0]
        action['question_ids'] = [qid]
        action['measurement']['baseline_metric_ids'] = [f'question.{qid}.target_mentions']
        action['measurement']['secondary_metric_ids'] = []
        action['target_asset']['mandatory_reuse_criteria'] = ['선정 질문의 직접 과업을 끝까지 설명할 수 있는지 본문으로 확인', '대상 브랜드와 연결된 작업 절차']
        for row in action['deliverables']: row['role'] = 'content'
        for row in action['steps']: row['role'] = 'content_work'
        plan['action_selection'] = {
            'question_id': qid, 'selected_candidate_id': 'reuse',
            'selection_reason': '기존 관련 자산을 먼저 확인하면 중복 문서 작성 비용을 줄일 수 있다.',
            'reversal_conditions': ['기존 후보가 모두 필수 기준을 충족하지 못하면 신규 자산으로 전환한다.'],
            'candidates': [
                {'candidate_id': cid, 'title': action['title'] if cid == 'reuse' else label,
                 'intervention_type': kind, 'effect_hypothesis': '선정 질문에 직접 답하는 근거를 마련한다.',
                 'evidence_ids': action['evidence_ids'], 'expected_benefit': '질문과 자산의 직접 연결',
                 'effort': {'size': size, 'assumptions': '콘텐츠 담당 1명이 후보 본문을 검토하는 작업을 가정한다.'},
                 'feasibility': 'needs_verification', 'prerequisites': action['prerequisites'] if cid == 'reuse' else ['본문 확인 필요'],
                 'risk': '본문이 미확인이라 적용 범위가 바뀔 수 있다.', 'disposition': status,
                 'decision_reason': reason}
                for cid,kind,label,size,status,reason in [
                    ('reuse','reuse_before_create','기존 자산 정비','small','selected','직접 관련 자산이 있으면 중복 작성을 피한다.'),
                    ('new','create_new','즉시 신규 작성','medium','deferred','본문 검토 없이 신규 작성하면 기존 자산과 중복될 수 있다.'),
                    ('verify','verify_workflow','제품 동작 검증','medium','rejected','제품 지원 여부는 후속 확인하되 선택한 고객 질문용 문서 산출물이 먼저 필요하다.')]
            ],
        }
        return plan


def agent(tmp_path, provider=None, store=None):
    return ProductAgent(EvidenceRepository(ROOT / 'VIVI_MVP_Sample_Dataset'), provider, tmp_path, store or Store())


def test_one_call_then_exact_cache_and_no_invented_review(tmp_path):
    provider = Provider(); runner = agent(tmp_path, provider)
    first = runner.run('tenant-a', 'lead'); second = runner.run('tenant-a', 'lead')
    assert first['result_type'] == 'propose' and second['result_type'] == 'cache_hit'
    assert provider.calls == 1 and first['usage']['model_calls'] == 1 and second['usage']['model_calls'] == 0
    assert first['requires_human_review'] and 'review' not in first
    assert first['execution_contracts'] == second['execution_contracts']
    assert first['events'] == ['load_evidence', 'policy_and_current_actions', 'exact_cache', 'generate_once', 'validate_and_compile']


@pytest.mark.parametrize('topic,expected', [('automation', 'no_action'), ('scale', 'insufficient_evidence')])
def test_policy_paths_do_not_call_model(tmp_path, topic, expected):
    provider = Provider(); result = agent(tmp_path, provider).run('demo', topic)
    assert result['result_type'] == expected and provider.calls == 0
    assert result['plan'] is None and result['usage']['model_calls'] == 0
    if topic == 'scale': assert len(result['evidence_summary']['excluded_pairs']) == 6


@pytest.mark.parametrize('mode', ['invalid', 'fail'])
def test_failure_is_not_retried_or_saved_as_valid_cache(tmp_path, mode):
    provider = Provider(**{mode: True}); runner = agent(tmp_path, provider)
    assert runner.run('demo', 'lead')['result_type'] == 'fallback'
    assert runner.run('demo', 'lead')['result_type'] == 'fallback'
    assert provider.calls == 1
    assert not list(tmp_path.glob('customers/*/cache/*.json'))


def test_cache_and_budget_are_tenant_scoped(tmp_path):
    provider = Provider(); runner = agent(tmp_path, provider)
    runner.run('tenant-a', 'lead'); runner.run('tenant-b', 'lead')
    assert provider.calls == 2
    assert len(list(tmp_path.glob('customers/*/usage.sqlite'))) == 2


def test_saved_actions_are_rechecked_before_cache(tmp_path):
    provider, store = Provider(), Store(); runner = agent(tmp_path, provider, store)
    runner.run('demo', 'lead')
    store.actions.append({'customer_id': 'demo', 'topic_id': 'lead', 'status': 'in_progress',
                          'question_ids': ['Q066', 'Q067', 'Q068', 'Q069', 'Q070']})
    assert runner.run('demo', 'lead')['result_type'] == 'no_action'
    assert provider.calls == 1
    store.actions[0]['status'] = 'dismissed'
    assert runner.run('demo', 'lead')['result_type'] == 'propose'
    assert provider.calls == 2


def test_model_setting_and_context_invalidate_cache(tmp_path):
    provider = Provider(); runner = agent(tmp_path, provider)
    runner.run('demo', 'lead')
    runner.run('demo', 'lead', context={'business_goal': 'new context'})
    provider.cache_identity = 'test-other-model'
    runner.run('demo', 'lead')
    assert provider.calls == 3


def test_missing_key_fallback_is_explicit(tmp_path):
    result = agent(tmp_path).run('demo', 'lead')
    assert result['result_type'] == 'fallback' and result['plan'] is None
    assert result['priority'] and result['usage']['model_calls'] == 0


def test_dataset_version_change_cannot_reuse_prior_result(tmp_path):
    import shutil
    data = tmp_path / 'dataset'
    shutil.copytree(ROOT / 'VIVI_MVP_Sample_Dataset', data)
    state = tmp_path / 'state'; provider, store = Provider(), Store()
    first = ProductAgent(EvidenceRepository(data), provider, state, store).run('demo', 'lead')
    manifest = data / 'manifest.json'
    value = json.loads(manifest.read_text()); value['version'] = 'changed-test-snapshot'
    manifest.write_text(json.dumps(value))
    second = ProductAgent(EvidenceRepository(data), provider, state, store).run('demo', 'lead')
    assert first['dataset_version'] != second['dataset_version']
    assert second['result_type'] == 'propose' and provider.calls == 2


def test_monthly_budget_fallback_prevents_a_new_scope_call(tmp_path, monkeypatch):
    from vivi_agent import product
    original = product.Ledger
    def small_budget(path, **kwargs):
        return original(path, **(kwargs | {'monthly_call_cap': 1}))
    monkeypatch.setattr(product, 'Ledger', small_budget)
    provider = Provider(); runner = agent(tmp_path, provider)
    assert runner.run('demo', 'lead')['result_type'] == 'propose'
    result = runner.run('demo', 'lead', context={'new_scope': True})
    assert result['result_type'] == 'fallback' and result['usage']['model_calls'] == 0
    assert provider.calls == 1


def test_actual_money_limit_rejects_before_assignment_charge(tmp_path, monkeypatch):
    monkeypatch.setenv('VIVI_ACTUAL_MAX_MONTHLY_COGS_USD', '10.515')
    provider = Provider()
    result = agent(tmp_path, provider).run('demo', 'lead')
    assert result['result_type'] == 'fallback' and provider.calls == 0
    assert result['usage']['ledger']['monthly_calls'] == 0
    assert 'ActualBudgetExceeded' in result['errors']


def test_actual_receipt_settlement_is_not_counted_twice_on_import(tmp_path):
    from decimal import Decimal
    provider = Provider(); runner = agent(tmp_path, provider)
    runner.run('demo', 'lead')
    result = runner.run('demo', 'lead', context={'business_goal': 'a new scope'})
    assert provider.calls == 2
    assert Decimal(result['usage']['actual_budget']['confirmed_actual_cost_usd']) == Decimal('.02')
    assert Decimal(result['usage']['actual_budget']['held_cost_usd']) == 0


def test_exact_cache_and_zero_call_policy_survive_actual_budget_exhaustion(tmp_path, monkeypatch):
    provider = Provider(); runner = agent(tmp_path, provider)
    assert runner.run('demo', 'lead')['result_type'] == 'propose'
    monkeypatch.setenv('VIVI_ACTUAL_MAX_MONTHLY_COGS_USD', '10.51')
    assert runner.run('demo', 'lead')['result_type'] == 'cache_hit'
    assert runner.run('demo', 'automation')['result_type'] == 'no_action'
    assert runner.run('demo', 'scale')['result_type'] == 'insufficient_evidence'
    assert runner.run('demo', 'lead', context={'new_scope': True})['result_type'] == 'fallback'
    assert provider.calls == 1


def test_unpriced_provider_is_never_called(tmp_path):
    provider = Provider(); provider.estimated_max_cost = None
    result = agent(tmp_path, provider).run('demo', 'lead')
    assert result['result_type'] == 'fallback' and provider.calls == 0
    assert result['errors'] == ['ActualPriceUnknown']


def test_accounting_start_failure_prevents_provider_call(tmp_path, monkeypatch):
    from vivi_agent.actual_budget import ActualBudget, ActualBudgetExceeded
    def refuse_start(self, reservation_id):
        raise ActualBudgetExceeded('Budget changed before invocation')
    monkeypatch.setattr(ActualBudget, 'mark_started', refuse_start)
    provider = Provider()
    result = agent(tmp_path, provider).run('demo', 'lead')
    assert result['result_type'] == 'fallback' and provider.calls == 0
    assert result['usage']['model_calls'] == 0
    assert result['usage']['accounting_error'] == 'ActualBudgetExceeded'
    assert float(result['usage']['actual_budget']['held_cost_usd']) == 0


def test_failed_binding_cannot_import_a_phantom_charge(tmp_path, monkeypatch):
    from vivi_agent.actual_budget import ActualBudget
    original = ActualBudget.bind_legacy
    def failed_binding(self, reservation_id, receipt_id):
        raise RuntimeError('Binding unavailable before provider invocation')
    monkeypatch.setattr(ActualBudget, 'bind_legacy', failed_binding)
    provider = Provider(); runner = agent(tmp_path, provider)
    first = runner.run('demo', 'lead')
    assert first['result_type'] == 'fallback' and provider.calls == 0
    assert first['usage']['ledger']['monthly_calls'] == 0
    assert first['usage']['ledger']['charged_cost_usd'] == 0
    monkeypatch.setattr(ActualBudget, 'bind_legacy', original)
    second = runner.run('demo', 'lead')
    assert second['result_type'] == 'propose' and provider.calls == 1
    assert second['usage']['actual_budget']['unknown_cost_calls'] == 0
    assert float(second['usage']['actual_budget']['held_cost_usd']) == 0
