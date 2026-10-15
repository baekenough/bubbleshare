"""JSON 형식 준수와 별개로 개선 액션의 선택 근거를 검증한다."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from vivi_agent.evidence import EvidenceRepository
from vivi_agent.graph import constrain_schema, prompt_evidence
from vivi_agent.selection import ProductPlan, validate_selection, render_problem_summary
from test_product_agent import Provider, agent

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sample():
    pack = EvidenceRepository(ROOT / 'VIVI_MVP_Sample_Dataset').build('lead')
    raw = Provider().generate('design', '\nINPUT JSON:\n' + json.dumps({'selected_question_id': 'Q068'}), {})
    return ProductPlan.model_validate(raw).model_dump(), pack


def test_comparison_covers_same_question_and_distinct_interventions(sample):
    plan, pack = sample
    assert validate_selection(plan, pack, 'Q068') == []
    for row in plan['action_selection']['candidates']:
        row['intervention_type'] = 'reuse_before_create'
    assert any('three different' in e for e in validate_selection(plan, pack, 'Q068'))


@pytest.mark.parametrize('mutation,expected', [
    ('wrong_question', 'selected question'), ('duplicate_id', 'unique'),
    ('two_selected', 'exactly one'), ('title_mismatch', 'title'),
    ('blocked_selection', 'blocked intervention'), ('missing_prerequisite', 'retained'),
    ('ready_with_unknowns', 'cannot be ready'), ('unknown_reference', 'unknown evidence'),
    ('execution_mode_mismatch', 'execution mode'), ('no_content_work', 'task-specific execution'),
])
def test_invalid_comparison_is_rejected(sample, mutation, expected):
    plan, pack = sample; selection = plan['action_selection']; choice = selection['candidates'][0]
    if mutation == 'wrong_question': selection['question_id'] = 'Q070'
    elif mutation == 'duplicate_id': selection['candidates'][1]['candidate_id'] = choice['candidate_id']
    elif mutation == 'two_selected': selection['candidates'][1]['disposition'] = 'selected'
    elif mutation == 'title_mismatch': choice['title'] = 'different work'
    elif mutation == 'blocked_selection': choice['feasibility'] = 'blocked'
    elif mutation == 'missing_prerequisite': choice['prerequisites'].append('required customer approval')
    elif mutation == 'ready_with_unknowns': choice['feasibility'] = 'ready'
    elif mutation == 'unknown_reference': choice['evidence_ids'] = ['invented']
    elif mutation == 'execution_mode_mismatch': choice['intervention_type'] = 'verify_workflow'
    elif mutation == 'no_content_work':
        for step in plan['actions'][0]['steps']: step['role'] = 'publication'
    assert any(expected in e for e in validate_selection(plan, pack, 'Q068'))


def test_summary_numbers_and_rejection_reasons_are_rendered_from_authoritative_fields(sample):
    plan, pack = sample
    plan['summary'] = '미언급 0/12; unsupported model quantitative prose'
    plan['rejected_alternatives'] = [{'title': 'Other question', 'rejection_reason': 'Q070'}]
    result = render_problem_summary(plan, pack, 'Q068')
    assert '자사 언급 0/3' in result['summary'] and '미언급 0/12' not in result['summary']
    assert len(result['rejected_alternatives']) == 2
    assert 'Q070' not in json.dumps(result['rejected_alternatives'])
    assert plan['summary'].startswith('미언급')


def test_product_asset_schema_constrains_ownership_and_support_role(sample):
    _, pack = sample
    schema = constrain_schema(ProductPlan.model_json_schema(), prompt_evidence(pack))
    assert 'SRC021' in schema['$defs']['ProductAsset']['properties']['source_ids']['items']['enum']
    assert 'SRC025' not in schema['$defs']['ProductAsset']['properties']['source_ids']['items']['enum']
    assert 'supporting_only' in schema['$defs']['AssetAssessment']['properties']['reuse_decision']['enum']


def test_bad_selection_never_causes_a_repair_call(tmp_path):
    class BlockedProvider(Provider):
        def generate(self, *args):
            result = super().generate(*args)
            result['action_selection']['candidates'][0]['feasibility'] = 'blocked'
            return result
    provider = BlockedProvider(); runner = agent(tmp_path, provider)
    first = runner.run('demo','lead'); second = runner.run('demo','lead')
    assert first['result_type'] == second['result_type'] == 'fallback'
    assert provider.calls == 1
    assert not list(tmp_path.glob('customers/*/cache/*.json'))


def test_internal_work_keeps_visibility_as_context_without_publication_schedule(tmp_path, sample):
    plan, pack = sample
    action = plan['actions'][0]
    choice = plan['action_selection']['candidates'][0]
    choice['intervention_type'] = 'verify_workflow'
    plan['action_selection']['candidates'][2]['intervention_type'] = 'observe'
    action['target_asset']['mode'] = 'operational'
    action['target_asset']['source_ids'] = []
    for assessment in action['target_asset']['assessments']:
        assessment['reuse_decision'] = 'supporting_only'
    rendered, contracts, errors = agent(tmp_path)._validated(plan, pack, 'Q068')
    assert errors == []
    assert contracts[0]['status'] == 'context_baseline_only'
    policy = contracts[0]['execution_policy']
    assert policy['publication_gate']['applicability'] == 'not_applicable'
    assert policy['follow_up_gate']['applicability'] == 'not_applicable'
    assert all(step['role'] == 'content_work' for step in rendered['actions'][0]['steps'])
    assert '노출 수집은 이 작업에서 시작하지 않습니다' in rendered['monitoring_plan']
    assert '작업 효과를 입증하지 않습니다' in rendered['monitoring_plan']


def test_selected_prerequisite_is_carried_into_action_without_paraphrase_matching(tmp_path, sample):
    plan, pack = sample
    condition = '고객 담당자가 직접 확인한 게시 권한 확보'
    plan['action_selection']['candidates'][0]['prerequisites'].append(condition)
    raw = deepcopy(plan)
    rendered, _, errors = agent(tmp_path)._validated(plan, pack, 'Q068')
    assert errors == []
    assert condition in rendered['actions'][0]['prerequisites']
    assert set(raw['actions'][0]['prerequisites']) <= set(rendered['actions'][0]['prerequisites'])
    assert plan == raw


def test_unresolved_candidate_conditions_cannot_be_displayed_as_ready(tmp_path, sample):
    plan, pack = sample
    choice = plan['action_selection']['candidates'][0]
    choice['feasibility'] = 'ready'
    plan['actions'][0]['readiness'] = 'ready_to_start'
    rendered, _, errors = agent(tmp_path)._validated(plan, pack, 'Q068')
    assert errors == []
    assert rendered['action_selection']['candidates'][0]['feasibility'] == 'needs_verification'
    assert rendered['actions'][0]['readiness'] == 'needs_input'
    assert choice['feasibility'] == 'ready'


def test_citation_only_gain_cannot_satisfy_the_brand_visibility_goal(tmp_path, sample):
    plan, pack = sample
    citation = 'question.Q068.target_owned_citations'
    plan['actions'][0]['measurement']['baseline_metric_ids'] = [citation]
    rendered, contracts, errors = agent(tmp_path)._validated(plan, pack, 'Q068')
    assert errors == []
    contract = contracts[0]
    assert {m['selector']['kind'] for m in contract['primary_metrics']} == {'mentions', 'recommendations'}
    assert citation in {m['metric_id'] for m in contract['secondary_metrics']}
    assert not contract['outcome_policy']['citation_only_improvement_is_success']
    assert 'target_owned_citations' not in rendered['actions'][0]['measurement']['positive_signal']
    assert all(m['denominator'] == 3 for m in contract['primary_metrics'])
