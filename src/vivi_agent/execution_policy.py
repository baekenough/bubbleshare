"""1회 호출 초안의 자산 역할과 실행 조건을 계약으로 만든다.

모델은 과업별 내용과 필수 재사용 기준을 정한다. 코드는 자산 선정 절차와 게시·측정
조건을 정한다. 계약 생성 자체가 본문 검사·사실 승인·게시를 수행하지는 않는다.
"""
from __future__ import annotations

from copy import deepcopy


VERSION = "execution-policy.v2"


def _strings(value, field, *, nonempty=False):
    if (not isinstance(value, list) or (nonempty and not value)
            or any(not isinstance(item, str) or not item.strip() for item in value)
            or len(value) != len(set(value))):
        raise ValueError(f"{field} must contain distinct, explicit strings")
    return list(value)


def compile_execution_policy(action: dict, pack: dict) -> dict:
    """자유 서술을 해석하지 않고 명시된 값으로 독립된 실행 계약을 만든다."""
    asset = action["target_asset"]
    operational = asset.get("mode") == "operational"
    target = pack["scope"]["target_brand_id"]
    sources = {source["source_id"]: source for source in pack["sources"]}
    owned = {sid for sid, source in sources.items()
             if source.get("is_brand_owned") is True and source.get("brand_id") == target}
    assessments = asset.get("assessments", [])
    assessed = [row["source_id"] for row in assessments]
    if len(assessed) != len(set(assessed)) or set(assessed) != owned:
        raise ValueError("Asset policy must assess every target-owned source exactly once")
    roles = {"inspect_for_reuse": [], "supporting_only": [], "exclude_for_this_task": []}
    for row in assessments:
        role = row["reuse_decision"]
        if role not in roles or not isinstance(row.get("rationale"), str) or not row["rationale"].strip():
            raise ValueError("Asset assessment requires an explicit role and substantive rationale")
        roles[role].append(row["source_id"])
    candidates = _strings(asset.get("source_ids"), "target_asset.source_ids")
    if set(candidates) != set(roles["inspect_for_reuse"]):
        raise ValueError("Only inspect_for_reuse sources can be editable candidates; supporting_only is not a target")
    criteria = _strings(asset.get("mandatory_reuse_criteria"),
                        "target_asset.mandatory_reuse_criteria", nonempty=bool(candidates) and not operational)
    if any(item.get("role") not in {"content", "operational_checklist"}
           for item in action.get("deliverables", [])):
        raise ValueError("Every production deliverable needs an explicit content/operational_checklist role")
    if any(item.get("role") not in {"content_work", "asset_selection", "publication"}
           for item in action.get("steps", [])):
        raise ValueError("Every production step needs an explicit operational/content_work role")

    policy = {
        "version": VERSION,
        "scope": deepcopy(pack["scope"]),
        "asset_selection": {
            "applicability": "applicable",
            "candidate_source_ids": candidates,
            "supporting_source_ids": roles["supporting_only"],
            "excluded_source_ids": roles["exclude_for_this_task"],
            "mandatory_criteria": [
                {"criterion_id": f"reuse_{i + 1}", "description": text}
                for i, text in enumerate(criteria)
            ],
            "assessment_state": "pending_body_inspection" if candidates else "no_reuse_candidates",
            "supporting_sources_are_editable": False,
            "required_inspection": "every_candidate_against_every_mandatory_criterion",
            "decision_table": [
                {"condition": "any_candidate_has_unknown_criterion", "decision": "inspect_remaining",
                 "may_select_or_create": False},
                {"condition": "candidate_fails_any_mandatory_criterion", "decision": "exclude_that_candidate",
                 "may_revise_failed_candidate": False},
                {"condition": "all_candidates_inspected_and_at_least_one_passes_all", "decision": "choose_one_passing_candidate_and_revise",
                 "may_create": False},
                {"condition": "all_candidates_inspected_and_none_pass_all", "decision": "create_new_asset",
                 "may_revise_failed_candidate": False},
            ],
            "tie_break": "Compare direct question fit and estimated edit effort among passing candidates; record the choice and rejection reasons. Do not infer unseen body content.",
        },
        "publication_gate": {
            "applicability": "applicable",
            "operator": "all",
            "required": ["asset_selection_complete", "task_content_acceptance_met",
                         "claims_verified_or_removed", "no_unresolved_public_placeholders"],
            "unknown_claim_resolution": ["verify_with_receipt", "remove_from_public_content"],
            "unresolved_essential_information": "hold_publication",
            "placeholder_scope": "internal_draft_or_verification_register_only",
            "maximum_unresolved_public_placeholders": 0,
        },
        "follow_up_gate": {
            "applicability": "applicable",
            "operator": "all",
            "required_fields": ["exact_published_url", "target_owned_source_id", "confirmed_publication_at"],
            "target_brand_id": target,
            "url_source_identity": "The registered source_id must identify the exact final published URL and belong to target_brand_id.",
            "registration_status": "completed",
            "candidate_ids_are_final_selection": False,
            "pending_registration_or_missing_field": "hold_follow_up_collection",
        },
        "provenance": {
            "mandatory_criteria": "model_selected_task_specific_criteria",
            "operational_rules": "deterministic_product_policy",
            "source_body_inspection_performed": False,
            "publication_or_collection_executed": False,
        },
    }
    if operational:
        policy["asset_selection"].update({
            "applicability": "not_applicable", "assessment_state": "not_applicable",
            "required_inspection": None, "decision_table": [], "tie_break": None,
        })
        policy["publication_gate"] = {
            "applicability": "not_applicable", "required": [],
            "reason": "This action validates internal work; it does not publish an asset.",
        }
        policy["follow_up_gate"] = {
            "applicability": "not_applicable", "required_fields": [],
            "reason": "Publication-anchored visibility measurement does not evaluate this internal action.",
        }
        policy["internal_work_validation"] = {
            "operator": "all", "required": ["task_acceptance_met", "verification_evidence_recorded"],
            "result": "Record verified findings, remaining unknowns, and the owner's next decision.",
            "visibility_improvement_inferred": False,
        }
    return deepcopy(policy)


def evaluate_asset_selection(policy: dict, inspections: list[dict]) -> dict:
    """본문을 추론하지 않고 명시된 검사 기록만 판정한다.

    기록 형식은 {source_id, criteria: {criterion_id: True | False | None}}이다.
    다른 후보가 통과했어도 누락이나 None이 있으면 선정을 보류한다. 실제 검사는 수행하지 않는다.
    """
    selection = policy["asset_selection"]
    if selection["applicability"] == "not_applicable":
        if inspections:
            raise ValueError("Asset inspections are not applicable to an operational action")
        return {"decision": "not_applicable", "pending_source_ids": [], "failed_source_ids": [],
                "passing_source_ids": [], "selectable_source_ids": []}
    candidates = selection["candidate_source_ids"]
    criteria = [item["criterion_id"] for item in selection["mandatory_criteria"]]
    records = {}
    for row in inspections:
        sid = row.get("source_id")
        if sid not in candidates or sid in records:
            raise ValueError("Inspection records must identify distinct editable candidates")
        answers = row.get("criteria")
        if not isinstance(answers, dict) or set(answers) - set(criteria):
            raise ValueError("Inspection has unknown or missing criteria mapping")
        if any(answer is not None and type(answer) is not bool for answer in answers.values()):
            raise ValueError("Inspection criteria must be True, False, or None")
        records[sid] = answers
    pending, failed, passing = [], [], []
    for sid in candidates:
        answers = records.get(sid, {})
        values = [answers.get(cid) for cid in criteria]
        if any(value is None for value in values):
            pending.append(sid)
        if any(value is False for value in values):
            failed.append(sid)
        elif values and all(value is True for value in values):
            passing.append(sid)
    decision = ("inspect_remaining" if pending else
                "choose_one_passing_candidate_and_revise" if passing else "create_new_asset")
    return {"decision": decision, "pending_source_ids": pending, "failed_source_ids": failed,
            "passing_source_ids": passing, "selectable_source_ids": passing if not pending else []}


def _render_selection(selection: dict) -> str:
    candidates = "·".join(selection["candidate_source_ids"]) or "없음"
    criteria = " / ".join(item["description"] for item in selection["mandatory_criteria"]) or "재사용 후보 없음"
    supporting = "·".join(selection["supporting_source_ids"]) or "없음"
    return (
        f"개정 후보({candidates})의 본문을 모두 열람하고 각 후보에 동일한 필수 기준({criteria})을 전부 적용합니다. "
        "후보 하나의 기준 미충족은 그 후보의 제외 사유이며, 해당 후보를 개정하거나 즉시 신규 작성으로 넘어가지 않습니다. "
        "남은 후보를 모두 검사한 뒤, 모든 기준을 충족한 후보가 있으면 질문 직접성과 개정 노력을 비교해 그중 1건만 개정합니다. "
        "모든 후보의 검사가 완료되고 전체 기준을 충족한 후보가 하나도 없을 때만 신규 자산을 작성합니다. "
        "미확인 기준이 남으면 자산 선택·신규 작성을 보류합니다. "
        f"보조 참고({supporting})는 근거·링크용이며 개정 후보와 최종 인용 추적 대상에 포함하지 않습니다."
    )


PUBLICATION_RULE = (
    "게시 전 제품·보안·연동 주장의 근거를 확인하고, 확인되지 않은 주장과 항목은 근거로 확정하거나 공개 원고에서 삭제합니다. "
    "필수 정보가 해결되지 않으면 게시를 보류합니다. [제품 확인 후 입력] 등 미확정 placeholder는 내부 초안·확인표에만 허용하며 "
    "최종 공개 원고에 하나도 남기지 않습니다. 미지원이 확인된 기능을 지원한다고 표현하지 않습니다."
)
FOLLOW_UP_RULE = (
    "후속 수집 전에 최종 게시물의 정확한 URL과 그 URL을 가리키는 자사 소유 source_id를 모두 확정하고 등록을 완료합니다. "
    "확정 게시일시도 기록합니다. URL 또는 source_id 중 하나만 있거나 후보 목록·등록 요청만 있는 상태로는 수집을 시작하지 않습니다. "
    "실제 선택한 최종 source_id만 자산 인용을 추적하며 보조 참고 자료는 최종 대상에서 제외합니다."
)
INTERNAL_WORK_RULE = (
    "이 액션은 내부 작업의 결과와 사실을 검증합니다. 작업별 완료 기준에 따라 확인된 결과·검증 근거·남은 미확인 항목을 기록하고 "
    "담당자의 다음 결정을 남깁니다. 문서 자산 선정·신규 작성·게시는 이 액션의 필수 절차가 아니며, "
    "게시 이후의 노출 측정은 해당하지 않습니다. 내부 작업 완료만으로 노출 개선이나 제품 기능 지원을 입증하지 않습니다."
)


def _checklist(policy: dict) -> str:
    selection = policy["asset_selection"]
    criteria = selection["mandatory_criteria"]
    lines = ["내부 실행 확인표 — 원고에 공개할 콘텐츠가 아닙니다.", "", "필수 재사용 기준"]
    lines.extend(f"- {row['criterion_id']}: {row['description']}" for row in criteria)
    lines += ["", "| 개정 후보 | 모든 기준의 판정과 본문 근거 | 미충족 시 조치 |", "|---|---|---|"]
    for sid in selection["candidate_source_ids"]:
        lines.append(f"| {sid} | 각 기준 통과/실패/미확인 및 근거 기록 | 이 후보 제외, 나머지 후보 검사 계속 |")
    if not selection["candidate_source_ids"]:
        lines.append("| 없음 | 재사용 후보 없음 | 신규 자산 결정 및 근거 기록 |")
    lines += ["", _render_selection(selection), "", "게시 승인 조건", PUBLICATION_RULE,
              "", "후속 수집 시작 조건", FOLLOW_UP_RULE]
    return "\n".join(lines)


def render_execution_policy(action: dict, policy: dict) -> dict:
    """코드가 담당하는 실행 항목을 채운 독립된 액션을 반환한다.

    콘텐츠 예시·내용 작업·모델의 선행 조건·콘텐츠 완료 기준은 보존한다.
    실행 항목으로 표시된 내용만 교체하며 원래 응답은 호출 영수증과 캐시에 남긴다.
    """
    result = deepcopy(action)
    if policy["asset_selection"]["applicability"] == "not_applicable":
        result["target_asset"]["selection_rule"] = INTERNAL_WORK_RULE
        result["deliverables"] = [item for item in result.get("deliverables", []) if item["role"] == "content"] + [{
            "role": "operational_checklist", "name": "내부 작업 결과 검증 확인표",
            "outline": ["작업별 완료 기준과 결과", "검증 근거와 미확인 항목", "담당자의 후속 결정"],
            "example_content": INTERNAL_WORK_RULE + "\n\n| 확인 항목 | 기록할 내용 |\n|---|---|\n"
                               "| 완료 기준 | 각 기준별 결과와 검증 근거 |\n"
                               "| 미확인 항목 | 추가 확인 담당자와 필요한 입력 |\n"
                               "| 후속 결정 | 확인 결과에 따른 결정과 근거 |",
        }]
        result["steps"] = [item for item in result.get("steps", []) if item["role"] == "content_work"]
        result["acceptance_criteria"] = list(dict.fromkeys([*result.get("acceptance_criteria", []), INTERNAL_WORK_RULE]))
        return result
    selection_rule = _render_selection(policy["asset_selection"])
    result["target_asset"]["selection_rule"] = selection_rule
    result["deliverables"] = [item for item in result.get("deliverables", []) if item["role"] == "content"] + [{
        "role": "operational_checklist", "name": "자산 선정·게시·후속 수집 실행 확인표",
        "outline": ["동일한 필수 기준으로 전체 후보 검사", "통과 후보 중 선택 또는 전체 실패 후 신규 작성",
                    "미확정 주장·placeholder 해소", "정확한 URL과 자사 source_id 등록 완료"],
        "example_content": _checklist(policy),
    }]
    content_steps = [item for item in result.get("steps", []) if item["role"] == "content_work"]
    candidates = policy["asset_selection"]["candidate_source_ids"]
    first_instruction = (
        f"후보({'·'.join(candidates)})를 모두 열람하고 필수 기준별 통과/실패/미확인을 기록한 뒤, "
        "아래 자산 선정 규칙으로 1건을 결정합니다. 보조 참고 자료는 개정 대상에서 제외합니다."
        if candidates else
        "기존 자산의 제외 근거를 확인하고 신규 문서 1건의 범위를 정합니다. 보조 참고 자료는 개정 대상에서 제외합니다."
    )
    result["steps"] = [{
        "role": "asset_selection", "instruction": first_instruction,
        "expected_output": "전체 후보×전체 필수 기준의 판정·본문 근거, 최종 자산 1건 또는 신규 결정, 선정·배제 사유",
    }, *content_steps, {
        "role": "publication", "instruction": PUBLICATION_RULE,
        "expected_output": "미확정 주장과 placeholder가 없는 승인 원고 및 게시 기록, 또는 구체적인 게시 보류 사유",
    }, {
        "role": "publication", "instruction": FOLLOW_UP_RULE,
        "expected_output": "동일 게시물을 가리키는 정확한 URL AND 자사 소유 source_id의 등록 완료 기록과 확정 게시일시",
    }]
    additions = {
        "prerequisites": ["최종 공개 원고의 미확정 주장 해소 및 정확한 URL·자사 소유 source_id 등록 담당 확인"],
        "acceptance_criteria": [selection_rule, PUBLICATION_RULE, FOLLOW_UP_RULE],
    }
    if policy["asset_selection"]["candidate_source_ids"]:
        additions["prerequisites"].insert(0, "전체 개정 후보의 본문과 필수 기준별 검사 근거 확보")
    for field, values in additions.items():
        result[field] = list(dict.fromkeys([*result.get(field, []), *values]))
    if result["prerequisites"] and result.get("readiness") == "ready_to_start":
        result["readiness"] = "needs_input"
    return result
