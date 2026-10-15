"""자산 선택 시 실제 보유 자산 목록이 누락되지 않는지 검증한다."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from vivi_agent.evidence import EvidenceRepository
from vivi_agent.validation import validate_plan


ROOT = Path(__file__).resolve().parents[1]
OWNED_IDS = {"SRC001", "SRC002", "SRC003", "SRC004", "SRC021", "SRC022"}
CANDIDATE_IDS = {"SRC001", "SRC003", "SRC022"}


@pytest.fixture
def inventory_case():
    pack = EvidenceRepository(ROOT / "VIVI_MVP_Sample_Dataset").build("lead")
    plan = json.loads((ROOT / "output/lead.v7.json").read_text())["plan"]
    asset = plan["actions"][0]["target_asset"]
    asset["source_ids"] = sorted(CANDIDATE_IDS)
    asset["selection_rule"] = (
        "SRC001, SRC003, SRC022 본문을 확인해 Q068 절차를 재사용할 수 있는 자산을 선택한다."
    )
    rationales = {
        "SRC001": "제품 기능 본문에서 웹 문의 등록 과업을 다룰 수 있는지 확인한다.",
        "SRC002": "요금 안내는 이번 웹 문의 등록 절차의 직접적인 대상이 아니다.",
        "SRC003": "활용 가이드 본문에 웹 문의 등록 절차를 넣을 수 있는지 확인한다.",
        "SRC004": "고객 사례는 설정 절차의 근거가 아니므로 이번 직접 편집에서 제외한다.",
        "SRC021": "보안 정책은 이번 웹 문의 등록 절차의 직접적인 대상이 아니다.",
        "SRC022": "리드 스코어링 가이드 본문에 등록 전후 흐름을 연결할 수 있는지 확인한다.",
    }
    asset["assessments"] = [
        {
            "source_id": source_id,
            "reuse_decision": (
                "inspect_for_reuse" if source_id in CANDIDATE_IDS else "exclude_for_this_task"
            ),
            "rationale": rationales[source_id],
        }
        for source_id in sorted(OWNED_IDS)
    ]
    return pack, plan


def test_all_six_real_owned_assets_can_be_assessed_without_claiming_body_access(inventory_case):
    pack, plan = inventory_case
    actual_owned = {
        source["source_id"]
        for source in pack["sources"]
        if source["is_brand_owned"] and source["brand_id"] == pack["scope"]["target_brand_id"]
    }
    assert actual_owned == OWNED_IDS
    assert pack["capabilities"]["body_access"] is False
    assert validate_plan(plan, pack) == []


def test_skipping_the_existing_scoring_guide_is_rejected(inventory_case):
    pack, plan = inventory_case
    asset = plan["actions"][0]["target_asset"]
    asset["assessments"] = [item for item in asset["assessments"] if item["source_id"] != "SRC022"]
    asset["source_ids"].remove("SRC022")
    errors = validate_plan(plan, pack)
    assert any("assess each target-owned source exactly once" in error for error in errors)


@pytest.mark.parametrize("source_id", ["SRC_DOES_NOT_EXIST", "SRC025"])
def test_unknown_or_competitor_assessment_cannot_replace_an_owned_asset(inventory_case, source_id):
    pack, plan = inventory_case
    asset = plan["actions"][0]["target_asset"]
    # 후보 집합을 유지해 자산 목록 검증만 분리한다.
    next(item for item in asset["assessments"] if item["source_id"] == "SRC002")["source_id"] = source_id
    errors = validate_plan(plan, pack)
    assert any("assess each target-owned source exactly once" in error for error in errors)


def test_duplicate_assessment_is_rejected_even_when_every_owned_id_is_present(inventory_case):
    pack, plan = inventory_case
    assessments = plan["actions"][0]["target_asset"]["assessments"]
    assessments.append(deepcopy(next(item for item in assessments if item["source_id"] == "SRC022")))
    errors = validate_plan(plan, pack)
    assert any("assess each target-owned source exactly once" in error for error in errors)


@pytest.mark.parametrize("mismatch", ["omitted_candidate", "included_excluded_asset"])
def test_selected_source_ids_must_match_the_assessed_candidates(inventory_case, mismatch):
    pack, plan = inventory_case
    source_ids = plan["actions"][0]["target_asset"]["source_ids"]
    if mismatch == "omitted_candidate":
        source_ids.remove("SRC022")
    else:
        source_ids.append("SRC002")
    errors = validate_plan(plan, pack)
    assert any("source_ids must match all inspect_for_reuse candidates" in error for error in errors)


@pytest.mark.parametrize("rationale", ["", " \n\t"])
def test_an_assessment_requires_a_nonblank_reason(inventory_case, rationale):
    pack, plan = inventory_case
    assessments = plan["actions"][0]["target_asset"]["assessments"]
    next(item for item in assessments if item["source_id"] == "SRC022")["rationale"] = rationale
    errors = validate_plan(plan, pack)
    assert any("substantive reuse/exclusion reason" in error for error in errors)
