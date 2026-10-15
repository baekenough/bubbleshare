"""기능 확인 근거가 명시된 기능과 경로에만 적용되는지 검증한다."""

from copy import deepcopy
from pathlib import Path

import pytest

from vivi_agent.evidence import EvidenceRepository


@pytest.fixture
def repository():
    return EvidenceRepository(Path(__file__).resolve().parents[1] / "VIVI_MVP_Sample_Dataset")


def test_receipted_unsupported_capability_becomes_explicit_negative_fact(repository):
    context = {
        "verified_web_form_auto_registration": False,
        "evidence_receipt": "product-check-unsupported",
        "origin": "provided_test_context",
    }
    result = repository.build("lead", context=context)["business_context"]
    assert result["verified_product_facts_status"] == "partially_verified"
    assert result["verified_product_facts"] == [{
        "statement": "웹 문의의 CRM 리드 자동 등록은 지원되지 않는 것으로 확인되었습니다.",
        "receipt_id": "product-check-unsupported",
        "origin": "provided_test_context",
    }]
    assert result["verified_web_form_auto_registration"] is False


def test_receipted_supported_route_is_preserved_without_native_or_trial_inference(repository):
    context = {
        "verified_web_form_auto_registration": True,
        "verified_route": "Approved integration route B",
        "evidence_receipt": "product-check-supported",
        "origin": "provided_test_context",
        "free_trial": "unknown",
    }
    result = repository.build("migration", context=context)["business_context"]
    fact = result["verified_product_facts"][0]
    assert "자동 등록 지원이 확인" in fact["statement"]
    assert "Approved integration route B" in fact["statement"]
    assert "네이티브 기능 지원을 추론하지 않습니다" in fact["statement"]
    assert fact["receipt_id"] == context["evidence_receipt"]
    assert result["free_trial"] == "unknown"
    assert result["verified_product_facts_status"] == "partially_verified"
    assert len(result["verified_product_facts"]) == 1


@pytest.mark.parametrize("receipt", [None, "", "   "])
@pytest.mark.parametrize("supported", [True, False])
def test_boolean_without_receipt_does_not_create_verified_fact(repository, receipt, supported):
    context = {"verified_web_form_auto_registration": supported, "evidence_receipt": receipt}
    result = repository.build("lead", context=context)["business_context"]
    assert result["verified_product_facts"] == []
    assert result["verified_product_facts_status"] == "unknown"


@pytest.mark.parametrize("unsupported_type", ["true", "false", 1, 0, None])
def test_receipt_does_not_turn_non_boolean_values_into_capability_facts(repository, unsupported_type):
    result = repository.build("lead", context={
        "verified_web_form_auto_registration": unsupported_type,
        "evidence_receipt": "unscoped-receipt",
    })["business_context"]
    assert result["verified_product_facts"] == []
    assert result["verified_product_facts_status"] == "unknown"


def test_existing_user_facts_and_input_context_are_preserved(repository):
    existing_fact = {"statement": "Another verified capability", "receipt_id": "other-receipt"}
    context = {
        "verified_product_facts": [existing_fact],
        "verified_web_form_auto_registration": True,
        "evidence_receipt": "capability-receipt",
    }
    original = deepcopy(context)
    pack = repository.build("lead", context=context)
    result = pack["business_context"]
    assert context == original
    assert result["verified_product_facts"][0] == existing_fact
    assert len(result["verified_product_facts"]) == 2
    assert "구현 경로와 네이티브 기능 지원 여부는 확인되지 않았습니다" in result["verified_product_facts"][1]["statement"]
    result["verified_product_facts"][0]["statement"] = "changed copy"
    assert context == original


def test_normalization_is_idempotent_and_affects_context_hash(repository):
    context = {
        "verified_web_form_auto_registration": True,
        "evidence_receipt": "capability-receipt",
        "verified_route": "Verified route",
    }
    first = repository.build("lead", context=context)
    second = repository.build("lead", context=first["business_context"])
    unsupported = repository.build("lead", context={**context, "verified_web_form_auto_registration": False})
    unverified = repository.build("lead", context={**context, "evidence_receipt": None})
    assert second["business_context"] == first["business_context"]
    assert second["context_hash"] == first["context_hash"]
    assert len({first["context_hash"], unsupported["context_hash"], unverified["context_hash"]}) == 3
    assert first["dataset_hash"] == unsupported["dataset_hash"] == unverified["dataset_hash"]
