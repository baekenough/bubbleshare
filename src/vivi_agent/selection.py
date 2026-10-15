"""1회 호출에서 같은 고객 문제의 액션 후보를 비교하는 타입과 검증."""
from copy import deepcopy
from typing import Literal

from pydantic import Field

from .models import ActionPlan, ActionBrief, Asset, Deliverable, Step, StrictModel


class Effort(StrictModel):
    size: Literal['small', 'medium', 'large']
    assumptions: str = Field(min_length=1, description='Scope, roles and time/resource assumptions, not a measured cost')


class Candidate(StrictModel):
    candidate_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    intervention_type: Literal['reuse_before_create', 'create_new', 'verify_workflow', 'product_change', 'observe']
    effect_hypothesis: str = Field(min_length=1, description='Observed problem → intervention → possible outcome; not proven uplift')
    evidence_ids: list[str] = Field(min_length=1)
    expected_benefit: str = Field(min_length=1, description='Qualitative decision benefit; do not invent expected revenue or uplift percentages')
    effort: Effort
    feasibility: Literal['ready', 'needs_verification', 'blocked']
    prerequisites: list[str]
    risk: str = Field(min_length=1)
    disposition: Literal['selected', 'deferred', 'rejected']
    decision_reason: str = Field(min_length=1, description='Trade-off against the other interventions for this SAME question, not another question ranking')


class ActionSelection(StrictModel):
    question_id: str
    candidates: list[Candidate] = Field(min_length=3, max_length=4)
    selected_candidate_id: str
    selection_reason: str = Field(min_length=1)
    reversal_conditions: list[str] = Field(min_length=1, description='Evidence/constraints changing the intervention strategy, not individual asset selection instructions. Never select one asset before all candidates pass through the common ALL-criteria inspection.')


class ProductAsset(Asset):
    mandatory_reuse_criteria: list[str] = Field(description='All conditions a candidate body must satisfy, including direct relevance to this exact customer question. All are mandatory, never any N. Empty for operational work or when there are no reuse candidates.')


class ProductDeliverable(Deliverable):
    role: Literal['content', 'operational_checklist'] = Field(description='content is preserved verbatim; operational_checklist is rendered by code from the execution contract')


class ProductStep(Step):
    role: Literal['content_work', 'asset_selection', 'publication'] = Field(description='Only task-specific content_work is preserved. Code renders asset selection and publication gates.')


class ProductAction(ActionBrief):
    target_asset: ProductAsset
    deliverables: list[ProductDeliverable]
    steps: list[ProductStep]


class ProductPlan(ActionPlan):
    action_selection: ActionSelection
    actions: list[ProductAction] = Field(min_length=1, max_length=1)


def base_plan(value):
    """기존 스키마를 넓히지 않고 참조·측정 검증에 사용할 형태로 투영한다."""
    result = deepcopy(value)
    result.pop('action_selection', None)
    for action in result['actions']:
        action['target_asset'].pop('mandatory_reuse_criteria', None)
        for row in action['deliverables'] + action['steps']:
            row.pop('role', None)
    return result


def validate_selection(plan, pack, question_id):
    selection = plan['action_selection']
    candidates = selection['candidates']
    errors = []
    if selection['question_id'] != question_id:
        errors.append('action_selection: candidates must solve the selected question')
    ids = [row['candidate_id'] for row in candidates]
    if len(ids) != len(set(ids)):
        errors.append('action_selection: candidate IDs must be unique')
    if len({row['intervention_type'] for row in candidates}) < 3:
        errors.append('action_selection: compare at least three different intervention types')
    selected = [row for row in candidates if row['disposition'] == 'selected']
    if len(selected) != 1 or selected[0]['candidate_id'] != selection['selected_candidate_id']:
        errors.append('action_selection: exactly one candidate must match selected_candidate_id')
    elif plan['actions']:
        choice, action = selected[0], plan['actions'][0]
        if choice['title'] != action['title']:
            errors.append('action_selection: selected title must match the executable action')
        modes = {'reuse_before_create': {'revise', 'verify_then_choose'}, 'create_new': {'create'},
                 'verify_workflow': {'operational'}, 'product_change': {'operational'}, 'observe': {'operational'}}
        if action['target_asset']['mode'] not in modes[choice['intervention_type']]:
            errors.append('action_selection: selected intervention and execution mode disagree')
        if choice['intervention_type'] == 'reuse_before_create' and not action['target_asset']['source_ids']:
            errors.append('action_selection: reuse requires actual owned candidates')
        if choice['feasibility'] == 'blocked':
            errors.append('action_selection: blocked intervention cannot be selected; choose a feasible preparation or verification action')
        if choice['feasibility'] == 'needs_verification' and action['readiness'] == 'ready_to_start':
            errors.append('action_selection: selected prerequisites must not be presented as ready')
        if set(choice['prerequisites']) - set(action['prerequisites']):
            errors.append('action_selection: selected prerequisites must be retained in the action')
    if not any(d['role'] == 'content' and len(d['example_content'].strip()) >= 40 for a in plan['actions'] for d in a['deliverables']):
        errors.append('action_selection: a useful task-specific content artifact is required')
    if not any(s['role'] == 'content_work' for a in plan['actions'] for s in a['steps']):
        errors.append('action_selection: task-specific execution steps are required')
    allowed = set(pack['allowed_evidence_ids'])
    for candidate in candidates:
        if set(candidate['evidence_ids']) - allowed:
            errors.append('action_selection: unknown evidence reference')
        if candidate['feasibility'] == 'ready' and candidate['prerequisites']:
            errors.append('action_selection: unresolved prerequisites cannot be ready')
    return errors


def retain_selected_prerequisites(plan):
    """선정 후보의 모든 조건을 의미 추론 없이 액션에 옮긴다.

    같은 조건의 다른 표현도 함께 남긴다. 원래 응답은 영수증과 캐시에서 대조할 수 있다.
    """
    result = deepcopy(plan)
    selection = result['action_selection']
    for candidate in selection['candidates']:
        if candidate['feasibility'] == 'ready' and candidate['prerequisites']:
            candidate['feasibility'] = 'needs_verification'
    choices = [row for row in selection['candidates']
               if row['disposition'] == 'selected' and row['candidate_id'] == selection['selected_candidate_id']]
    if len(choices) == 1 and len(result['actions']) == 1:
        action = result['actions'][0]
        action['prerequisites'] = list(dict.fromkeys([*choices[0]['prerequisites'], *action['prerequisites']]))
        if action['prerequisites'] and action['readiness'] == 'ready_to_start':
            action['readiness'] = 'needs_input'
    return result


def align_visibility_outcomes(action, question_id):
    """브랜드 언급·추천을 주 목표로 두고 인용 신호는 보조로 분류한다.

    모델이 선택한 지표와 분모는 모두 보존한다. 이 정규화 전에 참조 검증을 수행해야 한다.
    """
    measurement = action['measurement']
    original = list(measurement['baseline_metric_ids'])
    primary = [f'question.{question_id}.target_mentions', f'question.{question_id}.target_recommendations']
    measurement['baseline_metric_ids'] = primary
    measurement['secondary_metric_ids'] = list(dict.fromkeys(
        metric for metric in [*original, *measurement['secondary_metric_ids']] if metric not in primary))
    return {
        'version': 'brand-visibility.v1',
        'objective': 'Increase mentions or recommendations of the target brand for the selected question.',
        'model_selected_primary_ids': original,
        'citation_only_improvement_is_success': False,
    }


def render_problem_summary(plan, pack, question_id):
    """표시할 수치는 코드가 계산한다. 모델 원문은 유료 호출 영수증에 남는다."""
    result = deepcopy(plan)
    query = next(q['query'] for q in pack['questions'] if q['question_id'] == question_id)
    facts = []
    for suffix, label in [('target_mentions', '자사 언급'), ('target_recommendations', '자사 추천'), ('target_owned_citations', '자사 출처 인용')]:
        metric = pack['metrics'][f'question.{question_id}.{suffix}']
        facts.append(f"{label} {metric['numerator']}/{metric['denominator']}")
    result['summary'] = f"{question_id} ‘{query}’의 적격 응답 기준: " + ' · '.join(facts) + '입니다. ' + result['action_selection']['selection_reason']
    result['actions'][0]['priority_reason'] = result['action_selection']['selection_reason']
    result['rejected_alternatives'] = [{'title': row['title'], 'rejection_reason': row['decision_reason']}
                                       for row in result['action_selection']['candidates'] if row['disposition'] != 'selected']
    return result
