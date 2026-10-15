from copy import deepcopy
from pathlib import Path

from vivi_agent.evidence import EvidenceRepository
from vivi_agent.validation import validate_references, validate_review

ROOT = Path(__file__).resolve().parents[1]


def pack():
    return EvidenceRepository(ROOT / "VIVI_MVP_Sample_Dataset").build("lead")


def review(receipt):
    return {"verdict": "approve", "scores": {name: {"value": 2, "reason": "test"}
            for name in ("grounding", "relevance", "specificity", "feasibility", "verification")},
            "issues": [], "hard_violations": [], "rationale": "test",
            "claim_checks": [{"quote": "verified route", "subject": "target", "status": "verified",
                              "asserted_as_fact": True, "verification_ref": receipt}]}


def test_observation_cannot_be_substituted_for_editable_source():
    evidence = pack()
    observation = evidence["observations"][0]["observation_id"]
    assert validate_references({"source_ids": [observation]}, evidence)
    assert not validate_references({"evidence_ids": [observation]}, evidence)


def test_reviewer_cannot_invent_product_verification():
    assert validate_review(review(None), pack())
    assert validate_review(review("invented-receipt"), pack())


def test_verified_context_receipt_is_required_and_scoped_to_context():
    evidence = deepcopy(pack())
    evidence["business_context"].update({"verified_web_form_auto_registration": True,
                                         "evidence_receipt": "eval-product-check-001"})
    assert not validate_review(review("eval-product-check-001"), evidence)
    assert validate_review(review("different-receipt"), evidence)
