from copy import deepcopy
from pathlib import Path

import pytest

from vivi_agent.evidence import EvidenceRepository
from vivi_agent.graph import constrain_schema, prompt_evidence
from vivi_agent.integration import PHASES, validate_integration
from vivi_agent.models import ActionPlan


ROOT = Path(__file__).resolve().parents[1]
CONTEXT = {"verified_web_form_auto_registration": True,
           "verified_route": "Confirmed route B", "evidence_receipt": "check-route-b"}


def pack(context=None):
    return EvidenceRepository(ROOT / "VIVI_MVP_Sample_Dataset").build("lead", context=context or CONTEXT)


def action():
    return {"integration_procedure": {
        "verified_route": CONTEXT["verified_route"], "verification_ref": CONTEXT["evidence_receipt"],
        "prerequisites_unknown": ["Operator confirms form access, actual field IDs and duplicate policy"],
        "steps": [{"phase": phase, "input_example": "Fictional lead-demo@example.test inquiry",
                   "operation": "Submit through the verified route or compare observed records",
                   "expected_result": "Observed outcome must match the operator-confirmed policy",
                   "verification_artifact": "Submission ID/time and redacted CRM record comparison"}
                  for phase in sorted(PHASES)],
        "release_checks": ["Normal input equals recorded CRM fields", "Evidence and policy approval recorded"],
    }}


def test_verified_route_contract_preserves_known_identity():
    assert validate_integration(action(), pack()) == []
    schema = constrain_schema(ActionPlan.model_json_schema(), prompt_evidence(pack()))
    fields = schema["$defs"]["IntegrationProcedure"]["properties"]
    assert fields["verified_route"]["enum"] == [CONTEXT["verified_route"]]
    assert fields["verification_ref"]["enum"] == [CONTEXT["evidence_receipt"]]


@pytest.mark.parametrize("field,value", [("verified_route", "Confirmed route B [제품 확인 후 입력]"),
                                         ("verified_route", "native feature"),
                                         ("verification_ref", "different-proof")])
def test_known_route_cannot_be_deferred_changed_or_backed_by_another_receipt(field, value):
    candidate = action()
    candidate["integration_procedure"][field] = value
    assert validate_integration(candidate, pack())


@pytest.mark.parametrize("phase", sorted(PHASES))
def test_missing_observable_phase_cannot_pass_on_generic_steps(phase):
    candidate = action()
    candidate["integration_procedure"]["steps"] = [s for s in candidate["integration_procedure"]["steps"]
                                                  if s["phase"] != phase]
    assert any("missing phases" in error for error in validate_integration(candidate, pack()))


@pytest.mark.parametrize("field", ["input_example", "operation", "expected_result", "verification_artifact"])
def test_empty_step_contract_is_rejected(field):
    candidate = action()
    candidate["integration_procedure"]["steps"][0][field] = "   "
    assert validate_integration(candidate, pack())


@pytest.mark.parametrize("change", [{"verified_web_form_auto_registration": False},
                                   {"evidence_receipt": None}, {"verified_route": ""}])
def test_no_confirmed_procedure_when_unsupported_or_unverified(change):
    evidence = pack({**CONTEXT, **change})
    assert validate_integration(action(), evidence)
    assert validate_integration({"integration_procedure": None}, evidence) == []


def test_confirmed_route_requires_concrete_procedure():
    assert validate_integration({"integration_procedure": None}, pack())
