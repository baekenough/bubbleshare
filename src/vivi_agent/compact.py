"""한 번의 호출로 판단 초안을 생성하고 표시용 문장은 코드가 조립한다.

압축은 전달 형식만 바꾸며 원본 근거팩을 대체하지 않는다. 모델의 판단을 전체 계약으로
옮긴 뒤 기존 검증기와 정책 컴파일러를 적용한다.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Literal

from pydantic import Field

from .graph import constrain_schema, prompt_evidence
from .integration import verified_integration
from .models import AssetAssessment, IntegrationProcedure, Step, StrictModel
from .prompts import COMMON
from .selection import ActionSelection, Candidate, ProductPlan


VERSION = 'compact_action_draft.v1'


class CompactSelection(ActionSelection):
    candidates: list[Candidate] = Field(min_length=3, max_length=3)


class CompactAsset(StrictModel):
    proposed_title: str
    assessments: list[AssetAssessment] = Field(description='Every target-owned source exactly once: main edit candidate, supporting reference, or excluded; metadata-based relevance only')
    mandatory_reuse_criteria: list[str] = Field(description='ALL required body criteria for every main candidate, including direct relevance to the exact customer task and target brand; [] for operational work or no candidates')


class CompactDeliverable(StrictModel):
    name: str
    outline: list[str]
    example_content: str = Field(min_length=40, description='A usable task-specific draft, mapping/test table or internal work artifact; not instructions to create one')


class CompactAction(StrictModel):
    asset: CompactAsset
    owner_role: str
    deliverable: CompactDeliverable
    content_steps: list[Step] = Field(min_length=2, max_length=3, description='Task-specific work and expected outputs only; code supplies asset inspection and publication gates')
    acceptance_criteria: list[str] = Field(min_length=2, description='Concrete artifact quality and factual checks; do not repeat code-owned asset selection or publication gates')
    secondary_metric_ids: list[str] = Field(description='Optional current metrics for additional observation, each requiring its full denominator; [] if unnecessary. Selected-question target_mentions and target_recommendations are already primary.')
    integration_procedure: IntegrationProcedure | None


class CompactDraft(StrictModel):
    kind: Literal['compact_action_draft.v1']
    action_selection: CompactSelection
    action: CompactAction
    limitations: list[str]


INSTRUCTIONS = '''
SINGLE PRODUCTION CONTRACT — compact_action_draft.v1:
One call compares interventions and drafts the selected work for human review. There is
no independent reviewer or repair call. Return only CompactDraft, not a full ActionPlan.
priority ranks customer QUESTIONS, not action impact. Solve selected_question_id. Compare
exactly three meaningfully different intervention types for this SAME customer problem;
other questions are context/counterevidence, never alternative action candidates. For each,
connect observed problem → intervention → possible outcome, supplied evidence, qualitative
benefit, effort assumptions (scope, roles, time/resources), feasibility, prerequisites,
risk and the trade-off against the others. Do not invent uplift or revenue. Select exactly
one nonblocked candidate and explain why it wins now and what facts would reverse it.
Unresolved prerequisites mean needs_verification. A known false capability is unsupported,
not an invitation to verify it later. Existing actions are user state, not proof of effect;
do not duplicate them. Reasons should contain decision evidence, not chain-of-thought.

Assess EVERY target-owned source exactly once. inspect_for_reuse means a possible MAIN
editable asset for this exact end-to-end customer task. supporting_only means a supporting
reference, NEVER an edit/publication target. exclude_for_this_task means unrelated. Give
metadata-grounded reasons only; page bodies are unknown. Consider specialized guides as
well as general pages without presuming their contents. For main candidates provide ALL
mandatory body criteria, including direct task coverage and connection to the target brand.
Code renders a workflow requiring the operator to inspect ALL candidates against ALL criteria
before selecting any; no body inspection has occurred. Failed or unchecked candidates are
ineligible. Creation is allowed only when every candidate is checked and
NONE passes ALL criteria. Never weaken this to any-N, almost-passing, OR, or an early winner.
Do not repeat workflow branches in asset rationales, content, steps or reversal_conditions.
reversal_conditions concern intervention strategy. Use [] criteria for operational work
or no main candidates. reuse_before_create requires actual main candidates; its mode is
verify_then_choose. create_new becomes create. verify_workflow/product_change/observe become
operational internal work: no publication or visibility collection task is implied.

Provide ONE usable task artifact with actual copy/examples or a concrete internal-work/test
table. Answer the selected customer's task, not generic promotion. Include every promised
example. Give 2–3 content_steps with expected outputs and concrete acceptance_criteria.
The selected candidate supplies title, evidence, effort, prerequisites and effect hypothesis;
do not repeat these as extra action fields. Code supplies summary, rejected alternatives,
asset-selection/checklist/publication steps, readiness and measurement prose. Do not put
these operational checklists into the content artifact. Keep content-specific fact checks.
Unknown product claims may use explicit INTERNAL [확인 후 입력] draft placeholders. Before
publication every unsupported/unknown claim must be resolved or removed; hold publication
if that leaves no honest answer. Exact published URL AND confirmed target-owned source_id
AND confirmed publication are all required before follow-up; code enforces this conjunction.
A search question and AI answer do not verify origin, features, certification, price, trial,
competitor support, manual entry or integration. Respect verified facts and false/unknown
flags even in titles and literal copy. Never invent an alternative route or UI/API fields.

If execution_requirements.verified_integration exists, copy route and receipt EXACTLY and
provide six phases: input, field_mapping, normal_submission, duplicate_submission,
missing_required_field, reconcile. Each needs fictional input, an operation through that
route, an acceptance condition and observable evidence. Use operator-confirmed real field
IDs, permissions, duplicate/required-field policies as prerequisites_unknown; do not invent
them. Compare submitted input to actual CRM fields and reconcile submission/record identity.
Use available records or operator notes, never assume logs. Include a reusable test table
and release checks. These are future tests, not proof they ran. Known route support needs
no placeholder or re-verification. Otherwise integration_procedure MUST be null.

The action covers the ENTIRE selected question's eligible AI-channel observations; the model
cannot narrow its denominator. Code fixes primary outcomes to brand mentions and recommendations,
not citation alone. Optional secondary metrics require their full current sampling scope,
and cannot alone prove success. Cite metric IDs in qualitative text; do not calculate ratios.
Code schedules publication-based follow-up at day 7 and 14 and distinguishes internal work.
This is a draft, not executed work, proof of impact, or independent quality approval.

TRANSPORT: metric_descriptions maps description_id to the original metric description.
A mention/citation missing observation_id inherits its enclosing observation's ID. Evidence
reference choices are listed once in the schema. Relations are derivable by joining question,
observation, citation, source and brand IDs; all question observations remain as context.
'''

COLUMNAR_INSTRUCTIONS = '''
COLUMNAR TRANSPORT: evidence.table_columns gives readable field names for ordered row
arrays at each named table. mentions/citations are nested inside observation rows;
previous_comparison.pairs has its named header. metrics starts with metric_id, followed
by every original metric attribute. Row value i belongs to column i; null is an explicit
null, never a missing value. All observations, questions, sources, original IDs, text and
attributes are retained. A table without a header remains ordinary JSON objects.
'''


def _question_scope(pack: dict, question_id: str) -> tuple[list[str], list[str]]:
    if question_id not in {q['question_id'] for q in pack['questions']}:
        raise ValueError('Selected question is absent from the evidence pack')
    rows = [o for o in pack['observations'] if o['question_id'] == question_id]
    if not rows:
        raise ValueError('Selected question has no eligible observations')
    observation_ids = {o['observation_id'] for o in rows}
    primary = [f'question.{question_id}.target_mentions',
               f'question.{question_id}.target_recommendations']
    for metric_id in primary:
        metric = pack['metrics'].get(metric_id, {})
        denominator_ids = metric.get('observation_ids', [])
        if (set(denominator_ids) != observation_ids or len(denominator_ids) != len(observation_ids)
                or metric.get('denominator') != len(observation_ids)):
            raise ValueError(f'Selected question canonical denominator mismatch: {metric_id}')
    return sorted({o['channel'] for o in rows}), primary


def expand_draft(value: dict, pack: dict, question_id: str) -> dict:
    """모델의 판단과 내용을 보존하며 전체 ProductPlan을 조립한다.

    선택이 모호하면 변환을 거부한다. 실행 가능성의 모순이나 부적절한 문장 등은
    일반 검증 단계에서 판단할 수 있도록 그대로 남긴다.
    """
    draft = CompactDraft.model_validate(value).model_dump()
    selection = draft['action_selection']
    if selection['question_id'] != question_id:
        raise ValueError('Compact selection changed the selected question')
    chosen = [c for c in selection['candidates'] if c['disposition'] == 'selected']
    if len(chosen) != 1 or chosen[0]['candidate_id'] != selection['selected_candidate_id']:
        raise ValueError('Compact selection requires exactly one matching selected candidate')
    choice, work = chosen[0], draft['action']
    channels, primary = _question_scope(pack, question_id)
    mode = {'reuse_before_create': 'verify_then_choose', 'create_new': 'create',
            'verify_workflow': 'operational', 'product_change': 'operational',
            'observe': 'operational'}[choice['intervention_type']]
    asset = work['asset']
    source_ids = [a['source_id'] for a in asset['assessments'] if a['reuse_decision'] == 'inspect_for_reuse']
    limitations = list(draft['limitations'])
    missing = sorted({(o['question_id'], o['channel']) for o in pack['excluded'] if o['reason'] != 'superseded'})
    if missing:
        limitations.append('적격 응답이 없는 질문·채널은 결측으로 남기며 미노출로 계산하지 않습니다: '
                           + ', '.join(f'{qid}/{channel}' for qid, channel in missing))
    action = {
        'title': choice['title'], 'action_type': choice['intervention_type'],
        'question_ids': [question_id], 'channels': channels, 'evidence_ids': choice['evidence_ids'],
        'opportunity': choice['effect_hypothesis'], 'mechanism_hypothesis': choice['effect_hypothesis'],
        'target_asset': {**asset, 'mode': mode, 'source_ids': source_ids, 'selection_rule': ''},
        'owner_role': work['owner_role'],
        'deliverables': [{'role': 'content', **work['deliverable']}],
        'steps': [{'role': 'content_work', **step} for step in work['content_steps']],
        'prerequisites': choice['prerequisites'], 'acceptance_criteria': work['acceptance_criteria'],
        'measurement': {'baseline_metric_ids': primary, 'secondary_metric_ids': work['secondary_metric_ids'],
                        'follow_up': '', 'positive_signal': '', 'stop_or_revise': ''},
        'integration_procedure': work['integration_procedure'],
        'priority_reason': selection['selection_reason'], 'effort_assumptions': choice['effort']['assumptions'],
        'readiness': {'ready': 'ready_to_start', 'needs_verification': 'needs_input', 'blocked': 'blocked'}[choice['feasibility']],
    }
    return ProductPlan.model_validate({
        'decision': 'recommend', 'summary': selection['selection_reason'], 'actions': [action],
        'action_selection': selection,
        'rejected_alternatives': [{'title': c['title'], 'rejection_reason': c['decision_reason']}
                                  for c in selection['candidates'] if c['disposition'] != 'selected'],
        'monitoring_plan': '', 'data_tasks': [], 'limitations': limitations,
    }).model_dump()


def _compact_evidence(evidence: dict) -> dict:
    """근거를 생략하지 않고 기존 프롬프트 표현을 복원 가능한 형태로 압축한다."""
    result = deepcopy(evidence)
    result.pop('allowed_evidence_ids')  # 전체 참조 ID는 스키마에 남는다.
    descriptions: dict[str, str] = {}
    description_ids: dict[str, str] = {}
    for metric in result['metrics'].values():
        if 'description' in metric:
            description = metric.pop('description')
            if description not in description_ids:
                key = f'MD{len(descriptions) + 1:02}'
                description_ids[description] = key
                descriptions[key] = description
            metric['description_id'] = description_ids[description]
    result['metric_descriptions'] = descriptions
    for observation in result['observations']:
        for row in [*observation['mentions'], *observation['citations']]:
            if row.get('observation_id') == observation['observation_id']:
                row.pop('observation_id')
    return result


def encode_evidence_tables(evidence: dict) -> dict:
    """반복 키를 읽기 쉬운 열 이름으로 바꾸고 모든 값을 보존한다.

    필드 구성이 같은 행만 표로 만든다. 필드가 빠지거나 추가된 행이 있으면 해당 표는
    객체 형태를 유지해 누락과 null을 구분한다. 본문과 ID는 줄이거나 번호로 치환하지 않는다.
    """
    if 'table_columns' in evidence:
        raise ValueError('Evidence already contains the reserved table_columns key')
    result = deepcopy(evidence)
    columns: dict[str, list[str]] = {}

    def fields(rows):
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            return None
        names = list(rows[0])
        if any(not isinstance(row, dict) or set(row) != set(names) for row in rows):
            return None
        return names

    def table(name, rows):
        names = fields(rows)
        if names is None:
            return rows
        columns[name] = names
        return [[row[key] for key in names] for row in rows]

    observations = result['observations']
    for nested in ('mentions', 'citations'):
        rows = [row for observation in observations for row in observation[nested]]
        names = fields(rows)
        if names is not None:
            columns[nested] = names
            for observation in observations:
                observation[nested] = [[row[key] for key in names] for row in observation[nested]]
    for name in ('questions', 'observations', 'sources', 'excluded'):
        result[name] = table(name, result[name])
    metrics = result['metrics']
    if (isinstance(metrics, dict) and metrics
            and all(isinstance(metric, dict) and 'metric_id' not in metric for metric in metrics.values())):
        rows = [{'metric_id': key, **metric} for key, metric in metrics.items()]
        if fields(rows) is not None:
            result['metrics'] = table('metrics', rows)
    comparison = result['previous_comparison']
    if 'pairs' in comparison:
        comparison['pairs'] = table('previous_comparison.pairs', comparison['pairs'])
    return {'table_columns': columns, **result}


def decode_evidence_tables(evidence: dict) -> dict:
    """표 변환을 되돌려 원본과 정확히 일치하는지 검사할 때 사용한다."""
    result = deepcopy(evidence)
    if 'table_columns' not in result:
        return result
    columns = result.pop('table_columns')
    supported = {'questions', 'observations', 'sources', 'excluded', 'mentions', 'citations',
                 'metrics', 'previous_comparison.pairs'}
    if not isinstance(columns, dict) or set(columns) - supported:
        raise ValueError('Unknown evidence table header')
    for names in columns.values():
        if (not isinstance(names, list) or any(not isinstance(name, str) for name in names)
                or len(names) != len(set(names))):
            raise ValueError('Table columns must be distinct field names')

    def records(name, rows):
        if name not in columns:
            return rows
        names = columns[name]
        if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != len(names) for row in rows):
            raise ValueError(f'Table row width does not match its columns: {name}')
        return [dict(zip(names, row, strict=True)) for row in rows]

    for name in ('questions', 'observations', 'sources', 'excluded'):
        result[name] = records(name, result[name])
    for observation in result['observations']:
        for nested in ('mentions', 'citations'):
            observation[nested] = records(nested, observation[nested])
    if 'metrics' in columns:
        rows = records('metrics', result['metrics'])
        restored_metrics = {}
        for row in rows:
            metric_id = row.pop('metric_id')
            if not isinstance(metric_id, str) or metric_id in restored_metrics:
                raise ValueError('Metric rows need distinct original metric IDs')
            restored_metrics[metric_id] = row
        result['metrics'] = restored_metrics
    comparison = result['previous_comparison']
    if 'pairs' in comparison:
        comparison['pairs'] = records('previous_comparison.pairs', comparison['pairs'])
    return result


def _prune_definitions(schema: dict) -> None:
    """확인된 연동 경로로 스키마를 제한한 뒤 참조되는 정의만 남긴다."""
    definitions = schema['$defs']
    reachable: set[str] = set()

    def visit(node):
        if isinstance(node, dict):
            ref = node.get('$ref', '')
            if ref.startswith('#/$defs/'):
                name = ref.removeprefix('#/$defs/')
                if name not in reachable:
                    reachable.add(name)
                    visit(definitions[name])
            for key, child in node.items():
                if key != '$defs':
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(schema)
    schema['$defs'] = {key: value for key, value in definitions.items() if key in reachable}


def build_generation_input(pack: dict, question_id: str, existing_actions: list, priority: list) -> tuple[str, dict]:
    """단일 생성 지침에 토픽 전체 근거와 엄격한 출력 스키마를 결합한다."""
    channels, primary = _question_scope(pack, question_id)
    evidence = prompt_evidence(pack)
    schema = constrain_schema(CompactDraft.model_json_schema(), evidence)
    definitions = schema['$defs']
    # 다른 질문의 근거 ID까지 포함해 참조 목록을 공통 enum으로 정의한다.
    definitions['EvidenceId'] = {'type': 'string', 'enum': evidence['allowed_evidence_ids']}
    definitions['MetricId'] = {'type': 'string', 'enum': list(evidence['metrics'])}
    definitions['Candidate']['properties']['evidence_ids']['items'] = {'$ref': '#/$defs/EvidenceId'}
    definitions['CompactAction']['properties']['secondary_metric_ids']['items'] = {'$ref': '#/$defs/MetricId'}
    definitions['CompactSelection']['properties']['question_id']['enum'] = [question_id]
    owned = [s['source_id'] for s in evidence['sources'] if s.get('is_brand_owned')
             and s.get('brand_id') == evidence['scope']['target_brand_id']]
    assessments = definitions['CompactAsset']['properties']['assessments']
    assessments['minItems'] = assessments['maxItems'] = len(owned)
    if owned:
        definitions['OwnedSourceId'] = {'type': 'string', 'enum': owned}
        definitions['AssetAssessment']['properties']['source_id'] = {'$ref': '#/$defs/OwnedSourceId'}
    procedure = definitions['CompactAction']['properties']['integration_procedure']
    if verified_integration(evidence['business_context']):
        procedure.clear()
        procedure['$ref'] = '#/$defs/IntegrationProcedure'
        fields = definitions['IntegrationProcedure']['properties']
        fields['steps']['minItems'] = fields['steps']['maxItems'] = 6
        fields['prerequisites_unknown']['minItems'] = 1
        fields['release_checks']['minItems'] = 2
    else:
        procedure.clear()
        procedure['type'] = 'null'
    _prune_definitions(schema)
    payload = {
        'evidence': encode_evidence_tables(_compact_evidence(evidence)), 'priority': deepcopy(priority),
        'selected_question_id': question_id, 'existing_actions': deepcopy(existing_actions),
        'action_scope': {'question_ids': [question_id], 'eligible_channels': channels,
                         'primary_metric_ids': primary, 'model_may_narrow_scope': False},
    }
    prompt = COMMON + INSTRUCTIONS + COLUMNAR_INSTRUCTIONS + '\nINPUT JSON:\n' + json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    return prompt, schema
