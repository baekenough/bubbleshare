from pathlib import Path
import pytest

from vivi_agent.evidence import EvidenceRepository
from vivi_agent.validation import validate_diagnosis

REPO = EvidenceRepository(Path(__file__).resolve().parents[1] / "VIVI_MVP_Sample_Dataset")


def test_verified_context_receipt_can_ground_a_fact_and_be_read_by_the_agent():
    pack = REPO.build("lead", context={"origin": "synthetic_eval_fixture",
        "verified_web_form_auto_registration": False, "evidence_receipt": "eval-check-001"})
    diagnosis = {"decision": "recommend", "opportunity": "운영 대안 준비", "facts": [
        {"statement": "이 평가 맥락에서는 자동 등록을 지원하지 않음",
         "evidence_ids": ["eval-check-001"], "metric_ids": []}],
         "hypotheses": [], "evidence_requests": ["eval-check-001"], "limitations": []}
    assert not validate_diagnosis(diagnosis, pack)
    proof = REPO.lookup(pack, ["eval-check-001"])[0]
    assert proof["kind"] == "product_verification"
    assert proof["facts"][0]["origin"] == "synthetic_eval_fixture"


def test_customer_receipt_cannot_overwrite_dataset_evidence():
    with pytest.raises(ValueError, match="conflicts"):
        REPO.build("lead", context={"verified_product_facts": [
            {"receipt_id": "SRC022", "statement": "Override the existing source"}]})
