"""관측된 초안 3개의 실패 사례로 실행 정책의 회귀를 검증한다.

이전 초안은 실행 실패 문구를 재현하는 데만 사용한다.
운영 역할과 기준은 문장에서 추론하지 않고 명시적으로 제공한다.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from vivi_agent.evidence import EvidenceRepository
from vivi_agent.execution_policy import (
    compile_execution_policy, evaluate_asset_selection, render_execution_policy,
)
from vivi_agent.measurement import compile_measurement


ROOT = Path(__file__).resolve().parents[1]


def draft(case):
    value = json.loads((ROOT / f"output/product-{case}.json").read_text())
    action = deepcopy(value["plan"]["actions"][0])
    action["target_asset"]["mandatory_reuse_criteria"] = [
        "선택 질문의 직접 과업을 한 문서에서 답한다",
        "본문에서 목표 브랜드를 명시하고 필요한 검토 기준을 모두 제공한다",
    ]
    for item in action["deliverables"]:
        item["role"] = "content"
    for item in action["steps"]:
        item["role"] = "content_work"
    pack = EvidenceRepository(ROOT / "VIVI_MVP_Sample_Dataset").build(value["scope"]["topic_id"])
    return action, pack


def records(policy, result=True):
    selection = policy["asset_selection"]
    return [{"source_id": sid,
             "criteria": {item["criterion_id"]: result for item in selection["mandatory_criteria"]}}
            for sid in selection["candidate_source_ids"]]


def test_lead_supporting_reference_cannot_be_an_edit_or_measurement_candidate():
    action, pack = draft("lead")
    assessment = next(row for row in action["target_asset"]["assessments"] if row["source_id"] == "SRC021")
    assessment["reuse_decision"] = "supporting_only"
    with pytest.raises(ValueError, match="supporting_only is not a target"):
        compile_execution_policy(action, pack)
    action["target_asset"]["source_ids"].remove("SRC021")
    policy = compile_execution_policy(action, pack)
    assert policy["asset_selection"]["supporting_source_ids"] == ["SRC021"]
    assert "SRC021" not in policy["asset_selection"]["candidate_source_ids"]
    rendered = render_execution_policy(action, policy)
    assert "SRC021" not in compile_measurement(rendered, pack)["source_citation_check"]["source_ids"]
    with pytest.raises(ValueError, match="editable candidates"):
        evaluate_asset_selection(policy, [{"source_id": "SRC021", "criteria": {}}])


def test_security_failed_candidate_is_never_selectable_and_cannot_short_circuit_remaining():
    action, pack = draft("security")
    policy = compile_execution_policy(action, pack)
    inspections = records(policy)
    inspections[0]["criteria"]["reuse_1"] = False
    partial = evaluate_asset_selection(policy, inspections[:1])
    assert partial["decision"] == "inspect_remaining"
    assert partial["failed_source_ids"] == [inspections[0]["source_id"]]
    assert partial["selectable_source_ids"] == []
    complete = evaluate_asset_selection(policy, inspections)
    assert complete["decision"] == "choose_one_passing_candidate_and_revise"
    assert inspections[0]["source_id"] not in complete["selectable_source_ids"]
    assert len(complete["selectable_source_ids"]) == 2


def test_even_a_pass_does_not_skip_uninspected_other_candidates():
    action, pack = draft("security")
    policy = compile_execution_policy(action, pack)
    result = evaluate_asset_selection(policy, records(policy)[:1])
    assert result["passing_source_ids"]
    assert result["decision"] == "inspect_remaining"
    assert not result["selectable_source_ids"]


def test_create_requires_every_candidate_checked_and_no_fully_qualified_candidate():
    action, pack = draft("security")
    policy = compile_execution_policy(action, pack)
    inspections = records(policy, False)
    assert evaluate_asset_selection(policy, inspections[:-1])["decision"] == "inspect_remaining"
    assert evaluate_asset_selection(policy, inspections)["decision"] == "create_new_asset"
    inspections[-1]["criteria"]["reuse_2"] = None
    assert evaluate_asset_selection(policy, inspections)["decision"] == "inspect_remaining"


@pytest.mark.parametrize("bad_value", [1, 0, "true", "false", [], {}])
def test_inspection_does_not_treat_truthy_strings_or_numbers_as_passes(bad_value):
    action, pack = draft("security")
    policy = compile_execution_policy(action, pack)
    inspections = records(policy)
    inspections[0]["criteria"]["reuse_1"] = bad_value
    with pytest.raises(ValueError, match="True, False, or None"):
        evaluate_asset_selection(policy, inspections)


@pytest.mark.parametrize("mutation", ["duplicate", "unknown_criterion", "foreign_source"])
def test_malformed_inspection_cannot_qualify_assets(mutation):
    action, pack = draft("security")
    policy = compile_execution_policy(action, pack)
    inspections = records(policy)
    if mutation == "duplicate":
        inspections.append(deepcopy(inspections[0]))
    elif mutation == "unknown_criterion":
        inspections[0]["criteria"]["easier_criterion"] = True
    else:
        inspections[0]["source_id"] = "SRC025"
    with pytest.raises(ValueError):
        evaluate_asset_selection(policy, inspections)


def test_security_canonical_table_replaces_only_operational_deliverable_and_retains_specific_criteria():
    action, pack = draft("security")
    action["deliverables"][0]["role"] = "operational_checklist"
    action["steps"][0]["role"] = "asset_selection"
    action["steps"][-1]["role"] = "publication"
    action["target_asset"]["mandatory_reuse_criteria"].append("5개 이상의 보안 검토 항목을 한 문서에서 제공한다")
    original = deepcopy(action)
    policy = compile_execution_policy(action, pack)
    rendered = render_execution_policy(action, policy)
    assert action == original
    content = [item for item in rendered["deliverables"] if item["role"] == "content"]
    assert content == [original["deliverables"][1]]
    assert [item for item in rendered["steps"] if item["role"] == "content_work"] == original["steps"][1:-1]
    operational = [item for item in rendered["deliverables"] if item["role"] == "operational_checklist"]
    assert len(operational) == 1
    assert "5개 이상의 보안 검토 항목" in operational[0]["example_content"]
    assert "| 해당 섹션 개정 |" not in operational[0]["example_content"]
    assert "이 후보 제외, 나머지 후보 검사 계속" in operational[0]["example_content"]
    assert "모든 후보의 검사가 완료" in rendered["target_asset"]["selection_rule"]
    assert rendered["integration_procedure"] == original["integration_procedure"]


def test_unsupported_publication_requires_resolving_or_removing_placeholders_and_url_and_source_id():
    action, pack = draft("unsupported")
    action["deliverables"][1]["role"] = "operational_checklist"
    action["steps"][0]["role"] = "asset_selection"
    action["steps"][2]["role"] = "publication"
    action["steps"][3]["role"] = "publication"
    policy = compile_execution_policy(action, pack)
    gate = policy["publication_gate"]
    assert gate["operator"] == "all"
    assert gate["maximum_unresolved_public_placeholders"] == 0
    assert gate["unknown_claim_resolution"] == ["verify_with_receipt", "remove_from_public_content"]
    assert gate["unresolved_essential_information"] == "hold_publication"
    follow_up = policy["follow_up_gate"]
    assert follow_up["operator"] == "all"
    assert set(follow_up["required_fields"]) == {"exact_published_url", "target_owned_source_id", "confirmed_publication_at"}
    assert follow_up["registration_status"] == "completed"
    assert not follow_up["candidate_ids_are_final_selection"]
    rendered = render_execution_policy(action, policy)
    publication = [item for item in rendered["steps"] if item["role"] == "publication"]
    assert len(publication) == 2
    assert "공개 원고에서 삭제" in publication[0]["instruction"]
    assert "최종 공개 원고에 하나도 남기지" in publication[0]["instruction"]
    assert "모두 확정하고 등록을 완료" in publication[1]["instruction"]
    assert "URL AND 자사 소유 source_id" in publication[1]["expected_output"]
    assert rendered["deliverables"][0] == action["deliverables"][0]


@pytest.mark.parametrize("field", ["mandatory_reuse_criteria", "source_ids"])
def test_explicit_source_and_criteria_selections_cannot_be_omitted(field):
    action, pack = draft("security")
    del action["target_asset"][field]
    with pytest.raises(ValueError):
        compile_execution_policy(action, pack)


def test_empty_or_duplicate_mandatory_criteria_do_not_create_vacuously_passing_candidates():
    for invalid in [[], [""], ["same", "same"]]:
        action, pack = draft("security")
        action["target_asset"]["mandatory_reuse_criteria"] = invalid
        with pytest.raises(ValueError, match="mandatory_reuse_criteria"):
            compile_execution_policy(action, pack)


def test_source_inventory_cannot_drop_or_replace_owned_sources():
    action, pack = draft("lead")
    for mutation in ["drop", "duplicate", "competitor"]:
        bad = deepcopy(action)
        if mutation == "drop":
            bad["target_asset"]["assessments"].pop()
        elif mutation == "duplicate":
            bad["target_asset"]["assessments"].append(deepcopy(bad["target_asset"]["assessments"][0]))
        else:
            bad["target_asset"]["assessments"][0]["source_id"] = "SRC025"
        with pytest.raises(ValueError, match="every target-owned source"):
            compile_execution_policy(bad, pack)


def test_compile_and_render_are_detached_and_rendering_is_idempotent():
    action, pack = draft("security")
    policy = compile_execution_policy(action, pack)
    rendered = render_execution_policy(action, policy)
    assert render_execution_policy(rendered, policy) == rendered
    original_criteria = action["target_asset"]["mandatory_reuse_criteria"][:]
    policy["asset_selection"]["mandatory_criteria"][0]["description"] = "mutated"
    rendered["target_asset"]["source_ids"].clear()
    assert action["target_asset"]["mandatory_reuse_criteria"] == original_criteria
    assert action["target_asset"]["source_ids"]


def test_zero_candidates_can_choose_new_without_using_supporting_reference():
    action, pack = draft("lead")
    action["target_asset"]["source_ids"] = []
    action["target_asset"]["mandatory_reuse_criteria"] = []
    for row in action["target_asset"]["assessments"]:
        row["reuse_decision"] = "supporting_only"
    policy = compile_execution_policy(action, pack)
    result = evaluate_asset_selection(policy, [])
    assert result == {"decision": "create_new_asset", "pending_source_ids": [],
                      "failed_source_ids": [], "passing_source_ids": [], "selectable_source_ids": []}


@pytest.mark.parametrize("field", ["deliverables", "steps"])
def test_missing_operational_role_is_rejected_instead_of_guessing_from_text(field):
    action, pack = draft("lead")
    del action[field][0]["role"]
    with pytest.raises(ValueError, match="explicit"):
        compile_execution_policy(action, pack)


@pytest.mark.parametrize("before,after", [("ready_to_start", "needs_input"),
                                         ("needs_input", "needs_input"), ("blocked", "blocked")])
def test_added_unresolved_asset_inspection_downgrades_readiness_but_preserves_blocked(before, after):
    action, pack = draft("lead")
    action["prerequisites"] = []
    action["readiness"] = before
    rendered = render_execution_policy(action, compile_execution_policy(action, pack))
    assert "전체 개정 후보의 본문과 필수 기준별 검사 근거 확보" in rendered["prerequisites"]
    assert rendered["readiness"] == after
    assert action["readiness"] == before
    assert action["prerequisites"] == []


def operational_draft():
    action, pack = draft("lead")
    action["target_asset"]["mode"] = "operational"
    action["target_asset"]["source_ids"] = []
    action["target_asset"]["mandatory_reuse_criteria"] = []
    for item in action["target_asset"]["assessments"]:
        item["reuse_decision"] = "supporting_only"
    action["prerequisites"] = []
    action["acceptance_criteria"] = ["입력과 결과를 대조한 검증 기록을 남긴다"]
    action["readiness"] = "ready_to_start"
    action["steps"][0]["role"] = "asset_selection"
    action["steps"][-1]["role"] = "publication"
    action["deliverables"][-1]["role"] = "operational_checklist"
    return action, pack


def test_operational_action_does_not_invent_asset_creation_publication_or_visibility_gates():
    action, pack = operational_draft()
    policy = compile_execution_policy(action, pack)
    for key in ["asset_selection", "publication_gate", "follow_up_gate"]:
        assert policy[key]["applicability"] == "not_applicable"
    assert policy["asset_selection"]["decision_table"] == []
    assert policy["asset_selection"]["required_inspection"] is None
    assert policy["publication_gate"]["required"] == []
    assert policy["follow_up_gate"]["required_fields"] == []
    assert not policy["internal_work_validation"]["visibility_improvement_inferred"]
    assert evaluate_asset_selection(policy, [])["decision"] == "not_applicable"
    with pytest.raises(ValueError, match="not applicable"):
        evaluate_asset_selection(policy, [{"source_id": "SRC001", "criteria": {}}])
    rendered = render_execution_policy(action, policy)
    assert rendered["steps"] == [item for item in action["steps"] if item["role"] == "content_work"]
    assert rendered["deliverables"][:-1] == [item for item in action["deliverables"] if item["role"] == "content"]
    assert rendered["deliverables"][-1]["name"] == "내부 작업 결과 검증 확인표"
    assert "게시 이후의 노출 측정은 해당하지 않습니다" in rendered["target_asset"]["selection_rule"]
    assert rendered["prerequisites"] == []
    assert rendered["readiness"] == "ready_to_start"
    assert render_execution_policy(rendered, policy) == rendered


def test_new_content_without_reuse_candidates_has_no_impossible_body_inspection_prerequisite():
    action, pack = operational_draft()
    action["target_asset"]["mode"] = "create"
    policy = compile_execution_policy(action, pack)
    rendered = render_execution_policy(action, policy)
    assert policy["publication_gate"]["applicability"] == "applicable"
    assert "전체 개정 후보의 본문과 필수 기준별 검사 근거 확보" not in rendered["prerequisites"]
    assert rendered["readiness"] == "needs_input"


def test_first_step_is_a_compact_instruction_while_full_decision_table_remains_available():
    action, pack = draft("security")
    action["target_asset"]["mandatory_reuse_criteria"] = ["길고 상세한 도메인별 필수 조건 " * 30]
    rendered = render_execution_policy(action, compile_execution_policy(action, pack))
    first = rendered["steps"][0]["instruction"]
    assert len(first) < 180
    assert "SRC001·SRC003·SRC021" in first
    assert "모두 열람" in first and "통과/실패/미확인" in first
    assert "아래 자산 선정 규칙" in first
    assert action["target_asset"]["mandatory_reuse_criteria"][0] in rendered["target_asset"]["selection_rule"]
    assert "모든 후보의 검사가 완료" in rendered["deliverables"][-1]["example_content"]
