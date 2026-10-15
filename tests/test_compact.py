"""축약 생성이 판단과 근거를 보존하는지 오프라인으로 검증한다."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from vivi_agent.compact import (CompactDraft, VERSION, build_generation_input, expand_draft,
                                encode_evidence_tables, decode_evidence_tables, COLUMNAR_INSTRUCTIONS)
from vivi_agent.evidence import EvidenceRepository
from vivi_agent.execution_policy import compile_execution_policy, render_execution_policy
from vivi_agent.graph import constrain_schema, prompt_evidence
from vivi_agent.integration import PHASES, verified_integration
from vivi_agent.measurement import compile_measurement
from vivi_agent.models import ActionPlan
from vivi_agent.selection import ProductPlan, base_plan, validate_selection
from vivi_agent.validation import validate_plan


ROOT = Path(__file__).resolve().parents[1]
ROUTE = {'verified_web_form_auto_registration': True, 'verified_route': '운영자가 확인한 경로 B',
         'evidence_receipt': 'route-b-check'}


def pack(topic='lead', context=None):
    return EvidenceRepository(ROOT / 'VIVI_MVP_Sample_Dataset').build(topic, context=context)


def draft(evidence, question_id='Q068'):
    owned = [s for s in evidence['sources'] if s['is_brand_owned']
             and s['brand_id'] == evidence['scope']['target_brand_id']]
    observation = next(o for o in evidence['observations'] if o['question_id'] == question_id)
    candidates = []
    for index, kind in enumerate(('reuse_before_create', 'create_new', 'verify_workflow')):
        candidates.append({
            'candidate_id': f'candidate-{index}', 'title': ['선정 고객 과업 초안 보완', '신규 문서 작성', '제품 동작 확인'][index],
            'intervention_type': kind, 'effect_hypothesis': '고객의 구체적인 문의와 근거 문서의 연결을 검증한다.',
            'evidence_ids': [observation['observation_id']], 'expected_benefit': '직접 답변 가능한 자료 마련',
            'effort': {'size': 'small', 'assumptions': '콘텐츠 담당자 1명의 본문 검토 시간을 가정한다.'},
            'feasibility': 'needs_verification', 'prerequisites': ['후보 본문 및 실제 제품 지원 범위 확인'],
            'risk': '원문과 제품 사실이 확인되지 않아 작성 범위가 바뀔 수 있다.',
            'disposition': 'selected' if index == 0 else 'deferred',
            'decision_reason': '기존 근거 활용 가능성을 확인한 뒤 신규 작성 여부를 결정한다.',
        })
    procedure = None
    known = verified_integration(evidence['business_context'])
    if known:
        procedure = {**known, 'prerequisites_unknown': ['운영자가 실제 필드와 중복 정책을 확인한다.'],
                     'steps': [{'phase': phase, 'input_example': '가상 고객: demo@example.test / 상담 문의',
                                'operation': '확인된 경로 B를 통해 입력 또는 결과를 비교한다.',
                                'expected_result': '운영자 확인 정책과 실제 필드 값이 일치한다.',
                                'verification_artifact': '제출 식별자와 실제 CRM 기록의 필드 비교 기록'}
                               for phase in sorted(PHASES)],
                     'release_checks': ['입력과 실제 기록의 값 비교', '정책별 결과 및 운영자 승인 기록']}
    return {
        'kind': VERSION,
        'action_selection': {'question_id': question_id, 'candidates': candidates,
                             'selected_candidate_id': 'candidate-0',
                             'selection_reason': '본문 검토 후 기존 자산을 활용하면 중복 작업을 줄일 수 있다.',
                             'reversal_conditions': ['직접 답변 가능한 후보가 없다는 검토 결과이면 신규 작성 전략을 검토한다.']},
        'action': {'asset': {'proposed_title': '고객 문의 처리 범위와 확인 항목',
                            'assessments': [{'source_id': source['source_id'],
                                             'reuse_decision': 'inspect_for_reuse' if i == 0 else 'supporting_only',
                                             'rationale': '메타데이터상 고객 과업의 관련 자료이며 본문 확인이 필요하다.'}
                                            for i, source in enumerate(owned)],
                            'mandatory_reuse_criteria': ['선정 질문의 전체 과업에 직접 답할 수 있음', '대상 브랜드의 검증된 사실과 연결됨']},
                   'owner_role': '콘텐츠 담당자와 제품 담당자',
                   'deliverable': {'name': '고객 과업용 내부 초안', 'outline': ['상황', '확인할 입력과 결과'],
                                   'example_content': '가상 고객의 문의 예: 상담 담당자에게 문의 내용과 연락 정보를 전달하려고 합니다. 제품 지원 범위: [제품 확인 후 입력]. 실제 입력과 결과를 비교할 항목을 기록합니다.'},
                   'content_steps': [{'instruction': '확인된 고객 상황과 입력 예를 문서에 정리한다.', 'expected_output': '가상 예시와 확인된 사실을 구분한 초안'},
                                     {'instruction': '제품 담당자가 확인한 실제 처리 결과를 예시와 대조한다.', 'expected_output': '확인된 내용과 남은 사실 확인 항목'}],
                   'acceptance_criteria': ['고객 입력과 결과 예시가 서로 대응한다.', '제품 사실과 미확인 항목을 구별한다.'],
                   'secondary_metric_ids': [], 'integration_procedure': procedure},
        'limitations': ['AI 관측만으로 제품 기능을 검증할 수 없다.'],
    }


def payload(prompt, decode=True):
    value = json.loads(prompt.split('\nINPUT JSON:\n', 1)[1])
    if decode:
        value['evidence'] = decode_evidence_tables(value['evidence'])
    return value


def resolve(schema, value):
    while '$ref' in value:
        value = schema['$defs'][value['$ref'].removeprefix('#/$defs/')]
    return value


@pytest.mark.parametrize('context', [None, ROUTE, {'verified_web_form_auto_registration': False,
                                                'evidence_receipt': 'unsupported-check'}])
def test_expanded_draft_passes_existing_contracts_and_keeps_full_denominator(context):
    evidence = pack(context=context)
    value = draft(evidence)
    original = deepcopy(value)
    full = expand_draft(value, evidence, 'Q068')
    assert ProductPlan.model_validate(full)
    assert ActionPlan.model_validate(base_plan(full))
    assert validate_plan(base_plan(full), evidence) == []
    assert validate_selection(full, evidence, 'Q068') == []
    action = full['actions'][0]
    contract = compile_measurement(action, evidence)
    assert action['channels'] == ['ChatGPT', 'Gemini', 'Perplexity']
    assert all(metric['denominator'] == 3 for metric in contract['primary_metrics'])
    assert {m['selector']['kind'] for m in contract['primary_metrics']} == {'mentions', 'recommendations'}
    rendered = render_execution_policy(action, compile_execution_policy(action, evidence))
    assert any(step['role'] == 'publication' for step in rendered['steps'])
    assert rendered['deliverables'][0] == action['deliverables'][0]
    assert value == original
    full['action_selection']['candidates'][0]['evidence_ids'].append('detached-mutation')
    assert value == original


def test_expansion_copies_selected_fields_instead_of_generating_duplicate_answers():
    evidence = pack(); value = draft(evidence)
    selected = value['action_selection']['candidates'][0]
    selected['evidence_ids'].append('OBS0394')  # 다른 질문의 맥락도 보존한다.
    full = expand_draft(value, evidence, 'Q068'); action = full['actions'][0]
    assert action['title'] == selected['title']
    assert action['evidence_ids'] == selected['evidence_ids']
    assert action['prerequisites'] == selected['prerequisites']
    assert action['effort_assumptions'] == selected['effort']['assumptions']
    assert action['opportunity'] == selected['effect_hypothesis']
    assert action['mechanism_hypothesis'] == selected['effect_hypothesis']
    assert action['target_asset']['source_ids'] == [value['action']['asset']['assessments'][0]['source_id']]
    assert action['target_asset']['selection_rule'] == ''
    assert full['rejected_alternatives'] == [
        {'title': c['title'], 'rejection_reason': c['decision_reason']} for c in value['action_selection']['candidates'][1:]]
    assert full['monitoring_plan'] == ''


@pytest.mark.parametrize('kind,expected_mode', [('create_new', 'create'), ('verify_workflow', 'operational'),
                                               ('product_change', 'operational'), ('observe', 'operational')])
def test_selected_intervention_has_explicit_execution_mode(kind, expected_mode):
    evidence = pack(); value = draft(evidence)
    value['action_selection']['candidates'][0]['intervention_type'] = kind
    for assessment in value['action']['asset']['assessments']:
        assessment['reuse_decision'] = 'supporting_only'
    value['action']['asset']['mandatory_reuse_criteria'] = []
    action = expand_draft(value, evidence, 'Q068')['actions'][0]
    assert action['target_asset']['mode'] == expected_mode
    if expected_mode == 'operational':
        rendered = render_execution_policy(action, compile_execution_policy(action, evidence))
        assert all(step['role'] == 'content_work' for step in rendered['steps'])


@pytest.mark.parametrize('mutation', ['wrong_question', 'two_selected', 'wrong_selected_id'])
def test_ambiguous_selection_is_rejected_without_inventing_a_choice(mutation):
    evidence = pack(); value = draft(evidence)
    if mutation == 'wrong_question': value['action_selection']['question_id'] = 'Q070'
    elif mutation == 'two_selected': value['action_selection']['candidates'][1]['disposition'] = 'selected'
    else: value['action_selection']['selected_candidate_id'] = 'invented'
    with pytest.raises(ValueError): expand_draft(value, evidence, 'Q068')


def test_expansion_does_not_silently_repair_semantic_errors_or_unsafe_model_prose():
    evidence = pack(); value = draft(evidence)
    candidate = value['action_selection']['candidates'][0]
    candidate['feasibility'] = 'ready'
    value['action']['content_steps'][0]['instruction'] = '모델이 작성한 잘못된 OR 조건과 과장 문장을 그대로 남겨 검토한다.'
    full = expand_draft(value, evidence, 'Q068')
    assert full['action_selection']['candidates'][0]['feasibility'] == 'ready'
    assert full['actions'][0]['readiness'] == 'ready_to_start'
    assert full['actions'][0]['steps'][0]['instruction'] == value['action']['content_steps'][0]['instruction']
    assert any('cannot be ready' in error for error in validate_selection(full, evidence, 'Q068'))


@pytest.mark.parametrize('field', ['summary', 'rejected_alternatives', 'monitoring_plan', 'actions'])
def test_explicit_compact_discriminator_and_no_code_owned_generation_fields(field):
    evidence = pack(); value = draft(evidence)
    value[field] = [] if field != 'summary' else 'extra duplicate prose'
    with pytest.raises(ValidationError): CompactDraft.model_validate(value)
    value = draft(evidence); value.pop('kind')
    with pytest.raises(ValidationError): CompactDraft.model_validate(value)
    value = draft(evidence); value['kind'] = 'full_legacy_plan'
    with pytest.raises(ValidationError): CompactDraft.model_validate(value)


@pytest.mark.parametrize('field', ['channels', 'question_ids', 'title', 'prerequisites', 'effort_assumptions',
                                  'evidence_ids', 'priority_reason', 'measurement', 'steps'])
def test_model_cannot_supply_competing_copies_or_narrow_action_scope(field):
    value = draft(pack()); value['action'][field] = []
    with pytest.raises(ValidationError): CompactDraft.model_validate(value)


def test_inconsistent_canonical_question_denominator_is_not_silently_narrowed():
    evidence = pack(); value = draft(evidence)
    evidence['metrics']['question.Q068.target_mentions']['observation_ids'].pop()
    with pytest.raises(ValueError, match='canonical denominator mismatch'):
        expand_draft(value, evidence, 'Q068')
    with pytest.raises(ValueError, match='canonical denominator mismatch'):
        build_generation_input(evidence, 'Q068', [], [])


def test_missing_channel_is_explicit_and_never_added_as_an_absent_brand_response():
    evidence = pack('scale')
    question_id = next(q['question_id'] for q in evidence['questions']
                       if 0 < sum(o['question_id'] == q['question_id'] for o in evidence['observations']) < 3)
    full = expand_draft(draft(evidence, question_id), evidence, question_id)
    observed_channels = sorted({o['channel'] for o in evidence['observations'] if o['question_id'] == question_id})
    assert full['actions'][0]['channels'] == observed_channels
    assert len(observed_channels) < 3
    assert any('결측' in limitation for limitation in full['limitations'])
    assert full['data_tasks'] == []  # 실제 데이터 품질 분기는 생성 전에 재수집을 처리한다.


@pytest.mark.parametrize('topic,question_id', [('lead', 'Q068'), ('security', 'Q026')])
def test_transport_is_reversible_and_keeps_cross_question_evidence(topic, question_id):
    evidence = pack(topic)
    evidence['relations'] = {'reconstructible_duplicate': True}
    original = deepcopy(evidence)
    prompt, schema = build_generation_input(evidence, question_id, [], [])
    transported = payload(prompt)['evidence']
    assert 'relations' not in transported
    assert 'allowed_evidence_ids' not in transported
    restored = deepcopy(transported)
    descriptions = restored.pop('metric_descriptions')
    for metric in restored['metrics'].values():
        metric['description'] = descriptions[metric.pop('description_id')]
    for observation in restored['observations']:
        for row in [*observation['mentions'], *observation['citations']]:
            row.setdefault('observation_id', observation['observation_id'])
    restored['allowed_evidence_ids'] = schema['$defs']['EvidenceId']['enum']
    assert restored == prompt_evidence(evidence)
    assert evidence == original
    assert {o['observation_id'] for o in transported['observations']} == {o['observation_id'] for o in evidence['observations']}
    assert any(o['question_id'] != question_id for o in transported['observations'])
    assert transported['previous_comparison'] == prompt_evidence(evidence)['previous_comparison']


def test_transport_does_not_drop_nonredundant_nested_observation_identifier():
    evidence = pack()
    row = next(o for o in evidence['observations'] if o['mentions'])
    row['mentions'][0]['observation_id'] = 'nonmatching-source-identifier'
    prompt, _ = build_generation_input(evidence, 'Q068', [], [])
    transported = next(o for o in payload(prompt)['evidence']['observations'] if o['observation_id'] == row['observation_id'])
    assert transported['mentions'][0]['observation_id'] == 'nonmatching-source-identifier'


def test_contextual_shared_reference_enums_are_complete_and_resolvable():
    evidence = pack(); _, schema = build_generation_input(evidence, 'Q068', [], [])
    definitions = schema['$defs']
    assert definitions['CompactSelection']['properties']['question_id']['enum'] == ['Q068']
    assert definitions['CompactSelection']['properties']['candidates']['minItems'] == 3
    assert definitions['CompactSelection']['properties']['candidates']['maxItems'] == 3
    ids = resolve(schema, definitions['Candidate']['properties']['evidence_ids']['items'])['enum']
    assert ids == evidence['allowed_evidence_ids'] and 'OBS0394' in ids
    metrics = resolve(schema, definitions['CompactAction']['properties']['secondary_metric_ids']['items'])['enum']
    assert metrics == list(evidence['metrics'])
    owned = resolve(schema, definitions['AssetAssessment']['properties']['source_id'])['enum']
    assert 'SRC021' in owned and 'SRC025' not in owned
    assert 'supporting_only' in definitions['AssetAssessment']['properties']['reuse_decision']['enum']

    def walk(node):
        if isinstance(node, dict):
            if '$ref' in node: assert resolve(schema, node)
            for value in node.values(): walk(value)
        elif isinstance(node, list):
            for value in node: walk(value)
    walk(schema)


def test_known_route_stays_exact_and_all_six_phases_remain_in_schema():
    evidence = pack(context=ROUTE)
    _, schema = build_generation_input(evidence, 'Q068', [], [])
    property_schema = schema['$defs']['CompactAction']['properties']['integration_procedure']
    assert property_schema == {'$ref': '#/$defs/IntegrationProcedure'}
    fields = schema['$defs']['IntegrationProcedure']['properties']
    assert fields['verified_route']['enum'] == [ROUTE['verified_route']]
    assert fields['verification_ref']['enum'] == [ROUTE['evidence_receipt']]
    assert fields['steps']['minItems'] == fields['steps']['maxItems'] == 6
    assert set(schema['$defs']['IntegrationStep']['properties']['phase']['enum']) == PHASES
    value = draft(evidence)
    value['action']['integration_procedure']['verified_route'] = 'invented route'
    full = expand_draft(value, evidence, 'Q068')
    assert any('exact verified' in error for error in validate_plan(base_plan(full), evidence))


@pytest.mark.parametrize('context', [None, {'verified_web_form_auto_registration': False,
                                         'verified_route': 'unsupported route', 'evidence_receipt': 'false-check'}])
def test_unknown_or_unsupported_route_requires_null_and_omits_unused_procedure_schema(context):
    _, schema = build_generation_input(pack(context=context), 'Q068', [], [])
    assert schema['$defs']['CompactAction']['properties']['integration_procedure'] == {'type': 'null'}
    assert 'IntegrationProcedure' not in schema['$defs'] and 'IntegrationStep' not in schema['$defs']


def test_single_production_prompt_preserves_input_state_and_is_smaller_than_legacy_contract():
    from vivi_agent import prompts
    from vivi_agent.product_prompt import INSTRUCTIONS as LEGACY_PRODUCTION
    evidence = pack()
    actions = [{'title': '기존에 진행 중인 검토', 'status': 'in_progress', 'notes': '사용자가 추가한 조건'}]
    priority = [{'question_id': 'Q068', 'score': 1, 'reason': '질문 우선순위'}]
    before = deepcopy((evidence, actions, priority))
    prompt, schema = build_generation_input(evidence, 'Q068', actions, priority)
    data = payload(prompt)
    assert data['existing_actions'] == actions and data['priority'] == priority
    assert data['action_scope']['model_may_narrow_scope'] is False
    assert prompt.count('SINGLE PRODUCTION CONTRACT') == 1
    assert prompts.DESIGN not in prompt and LEGACY_PRODUCTION not in prompt
    old_payload = {'evidence': prompt_evidence(evidence), 'selected_question_id': 'Q068',
                   'existing_actions': actions, 'priority': priority}
    old_prompt = prompts.render('design', old_payload) + LEGACY_PRODUCTION
    old_schema = constrain_schema(ProductPlan.model_json_schema(), old_payload['evidence'])
    # 문자 수만 비교한다. 토큰 수나 비용 절감 추정치는 아니다.
    assert len(prompt) + len(json.dumps(schema)) < len(old_prompt) + len(json.dumps(old_schema))
    assert (evidence, actions, priority) == before


@pytest.mark.parametrize('phase', sorted(PHASES))
def test_compact_expansion_preserves_incomplete_integration_as_a_validation_failure(phase):
    evidence = pack(context=ROUTE); value = draft(evidence)
    procedure = value['action']['integration_procedure']
    procedure['steps'] = [step for step in procedure['steps'] if step['phase'] != phase]
    full = expand_draft(value, evidence, 'Q068')
    assert full['actions'][0]['integration_procedure'] == procedure
    assert any('missing phases' in error for error in validate_plan(base_plan(full), evidence))


def test_compact_expansion_does_not_convert_support_references_into_main_edit_targets():
    evidence = pack(); value = draft(evidence)
    for assessment in value['action']['asset']['assessments']:
        assessment['reuse_decision'] = 'supporting_only'
    full = expand_draft(value, evidence, 'Q068')
    assert full['actions'][0]['target_asset']['source_ids'] == []
    assert any('reuse requires actual owned candidates' in error for error in validate_selection(full, evidence, 'Q068'))
    assert all(assessment['reuse_decision'] == 'supporting_only'
               for assessment in full['actions'][0]['target_asset']['assessments'])


def test_compact_keeps_missing_mandatory_criteria_as_an_error_not_a_generic_replacement():
    evidence = pack(); value = draft(evidence)
    value['action']['asset']['mandatory_reuse_criteria'] = []
    full = expand_draft(value, evidence, 'Q068')
    assert full['actions'][0]['target_asset']['mandatory_reuse_criteria'] == []
    with pytest.raises(ValueError, match='criteria'):
        compile_execution_policy(full['actions'][0], evidence)


@pytest.mark.parametrize('case', ['lead', 'unsupported', 'confirmed', 'security'])
def test_prior_real_artifacts_round_trip_through_compact_without_losing_core_content(case):
    """투영 호환성만 검증한다. 새 모델의 품질 통과를 뜻하지 않는다."""
    context = (json.loads((ROOT / f'evals/contexts/lead_capability_{case}.json').read_text())
               if case in {'confirmed', 'unsupported'} else None)
    evidence = pack('security' if case == 'security' else 'lead', context)
    prior = json.loads((ROOT / f'output/product-v2-final/{case}.json').read_text())['plan']
    action = prior['actions'][0]
    content = next(d for d in action['deliverables'] if d['role'] == 'content')
    value = {'kind': VERSION, 'action_selection': prior['action_selection'],
             'action': {'asset': {key: action['target_asset'][key]
                                  for key in ['proposed_title', 'assessments', 'mandatory_reuse_criteria']},
                        'owner_role': action['owner_role'],
                        'deliverable': {key: val for key, val in content.items() if key != 'role'},
                        'content_steps': [{key: val for key, val in step.items() if key != 'role'}
                                          for step in action['steps'] if step['role'] == 'content_work'],
                        'acceptance_criteria': action['acceptance_criteria'],
                        'secondary_metric_ids': action['measurement']['secondary_metric_ids'],
                        'integration_procedure': action['integration_procedure']},
             'limitations': prior['limitations']}
    qid = prior['action_selection']['question_id']
    full = expand_draft(value, evidence, qid)
    assert validate_plan(base_plan(full), evidence) == []
    assert validate_selection(full, evidence, qid) == []
    compile_measurement(full['actions'][0], evidence)
    compile_execution_policy(full['actions'][0], evidence)
    assert full['action_selection'] == prior['action_selection']
    assert full['actions'][0]['deliverables'] == [content]
    assert full['actions'][0]['target_asset']['assessments'] == action['target_asset']['assessments']
    assert full['actions'][0]['target_asset']['mandatory_reuse_criteria'] == action['target_asset']['mandatory_reuse_criteria']
    assert full['actions'][0]['integration_procedure'] == action['integration_procedure']


def test_columnar_round_trip_preserves_all_fifteen_topic_projections():
    repository = EvidenceRepository(ROOT / 'VIVI_MVP_Sample_Dataset')
    questions = json.loads((ROOT / 'VIVI_MVP_Sample_Dataset/questions.json').read_text())
    topics = {question['topic_id'] for question in questions}
    assert len(topics) == 15
    for topic in topics:
        evidence = prompt_evidence(repository.build(topic))
        before = deepcopy(evidence)
        encoded = encode_evidence_tables(evidence)
        restored = decode_evidence_tables(encoded)
        assert restored == evidence == before, topic
        assert len(restored['questions']) == len(evidence['questions'])
        assert len(restored['observations']) == len(evidence['observations'])
        assert len(restored['sources']) == len(evidence['sources'])
        restored['scope']['topic_id'] = 'detached-mutated-value'
        assert evidence == before


@pytest.mark.parametrize('table', ['questions', 'observations', 'sources', 'metrics', 'mentions', 'citations',
                                  'previous_comparison.pairs'])
def test_nonuniform_attributes_fall_back_without_conflating_missing_with_null(table):
    evidence = prompt_evidence(pack())
    if table == 'metrics':
        records = list(evidence['metrics'].values())
    elif table == 'previous_comparison.pairs':
        records = evidence['previous_comparison']['pairs']
    elif table in {'mentions', 'citations'}:
        records = [row for observation in evidence['observations'] for row in observation[table]]
    else:
        records = evidence[table]
    records[0]['unusual_attribute'] = None
    records[1]['unusual_attribute'] = {'empty': '', 'zero': 0, 'false': False, 'text': '표시값\n원문 ✅'}
    # 나머지 행에서는 해당 속성을 의도적으로 생략한다.
    encoded = encode_evidence_tables(evidence)
    assert table not in encoded['table_columns']
    assert decode_evidence_tables(encoded) == evidence


def test_existing_metric_id_attribute_and_empty_tables_are_not_overwritten():
    evidence = prompt_evidence(pack())
    next(iter(evidence['metrics'].values()))['metric_id'] = 'an original nested attribute'
    for name in ('sources', 'questions', 'observations', 'excluded'):
        evidence[name] = []
    evidence['previous_comparison']['pairs'] = []
    encoded = encode_evidence_tables(evidence)
    assert encoded['table_columns'] == {}
    assert decode_evidence_tables(encoded) == evidence
    evidence['metrics'] = {}
    assert decode_evidence_tables(encode_evidence_tables(evidence)) == evidence


@pytest.mark.parametrize('malformation', ['short_row', 'long_row', 'duplicate_column', 'unknown_table', 'duplicate_metric_id'])
def test_decoder_rejects_malformed_tables_instead_of_losing_attributes(malformation):
    encoded = encode_evidence_tables(prompt_evidence(pack()))
    if malformation == 'short_row': encoded['sources'][0].pop()
    elif malformation == 'long_row': encoded['sources'][0].append('unexpected')
    elif malformation == 'duplicate_column': encoded['table_columns']['sources'][1] = encoded['table_columns']['sources'][0]
    elif malformation == 'unknown_table': encoded['table_columns']['unrecognized'] = ['field']
    else: encoded['metrics'][1][0] = encoded['metrics'][0][0]
    with pytest.raises(ValueError): decode_evidence_tables(encoded)


def test_table_header_key_collision_is_explicit_not_silent_attribute_loss():
    evidence = prompt_evidence(pack()); evidence['table_columns'] = {'preexisting': 'metadata'}
    with pytest.raises(ValueError, match='reserved table_columns'):
        encode_evidence_tables(evidence)


@pytest.mark.parametrize('case', ['lead', 'unsupported', 'confirmed', 'security'])
def test_columnar_inputs_keep_every_value_and_reduce_total_characters_by_at_least_fifteen_percent(case):
    context = (json.loads((ROOT / f'evals/contexts/lead_capability_{case}.json').read_text())
               if case in {'confirmed', 'unsupported'} else None)
    evidence = pack('security' if case == 'security' else 'lead', context)
    qid = 'Q026' if case == 'security' else 'Q068'
    prompt, schema = build_generation_input(evidence, qid, [], [])
    raw, restored = payload(prompt, decode=False), payload(prompt)
    assert raw['evidence']['table_columns']['metrics'][0] == 'metric_id'
    assert raw['evidence']['table_columns']['sources'] == list(evidence['sources'][0])
    assert raw['evidence']['table_columns']['observations'][0] == 'observation_id'
    assert restored['evidence']['business_context'] == evidence['business_context']
    assert restored['evidence']['sources'] == evidence['sources']
    assert {row['observation_id'] for row in restored['evidence']['observations']} == {
        row['observation_id'] for row in evidence['observations']}
    assert all(isinstance(row[0], str) and row[0].startswith('SRC') for row in raw['evidence']['sources'])
    assert COLUMNAR_INSTRUCTIONS in prompt
    prefix = prompt.split('\nINPUT JSON:\n', 1)[0].replace(COLUMNAR_INSTRUCTIONS, '')
    size = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
    before = len(prefix + '\nINPUT JSON:\n') + size(restored) + size(schema)
    after = len(prompt) + size(schema)
    assert (before - after) / before >= .15
