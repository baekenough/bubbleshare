import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vivi_agent.evaluation import evaluate
from vivi_agent.evidence import EvidenceRepository
from vivi_agent.graph import digest
from vivi_agent.ledger import Ledger
from vivi_agent.provider import ProviderError


DATASET = Path(__file__).resolve().parents[1] / "VIVI_MVP_Sample_Dataset"


def passing_judge():
    return {"meets_expectations": True, "satisfied": ["actionable deliverable"],
            "missed": [], "forbidden_claims": [], "rationale": "Independent fixture judgment."}


def rejecting_review():
    return {
        "verdict": "revise",
        "scores": {name: {"value": 1, "reason": "Missing useful content"} for name in
                   ("grounding", "relevance", "specificity", "feasibility", "verification")},
        "hard_violations": [], "claim_checks": [],
        "issues": [{"path": "deliverables", "problem": "No sample", "required_change": "Include a draft"}],
        "rationale": "Cannot execute the generic suggestion.",
    }


def generic_case():
    return {"id": "reject_generic_action", "topic_id": "lead", "snapshot_id": "SNAP-2",
            "stage": "review_actions", "candidate_override": {"title": "Make good content"},
            "expected": ["Identify missing usable deliverables and measurement"],
            "forbidden": ["Accept based only on valid evidence ID"]}


class FakeRunner:
    """실제 근거·원장과 통제된 생성·평가 응답으로 검증한다. 네트워크는 사용하지 않는다."""
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.repository = EvidenceRepository(DATASET)
        self.provider = SimpleNamespace(model="test-only", last_receipt={})
        self.ledger = Ledger(self.state_dir / "ledger.sqlite")
        self.judge = passing_judge()
        self.review_response = rejecting_review()
        self.result = {
            "status": "completed", "decision": "recommend",
            "plan": {"decision": "recommend", "summary": "actor draft"},
            "review": {"verdict": "approve", "scores": {"self_assigned": 10}},
            "validation_errors": [],
        }
        self.run_inputs = []
        self.paid_calls = []
        self.error = None

    def key(self, pack):
        return digest({"scope": pack["scope"], "context": pack["context_hash"]})

    def run(self, topic, snapshot, context):
        self.run_inputs.append((topic, snapshot, context))
        return copy.deepcopy(self.result)

    def call(self, key, stage, output_type, payload):
        request_hash = digest(payload)
        cached = self.ledger.get_completed(key, stage, request_hash)
        if cached is not None:
            return cached
        receipt = self.ledger.reserve(key, stage, request_hash)
        self.paid_calls.append((key, stage, copy.deepcopy(payload)))
        if self.error:
            self.ledger.fail(receipt, "ProviderError")
            raise self.error
        result = output_type.model_validate(self.review_response if stage == "review" else self.judge).model_dump()
        self.ledger.complete(receipt, result, {})
        return result


def normal_case(case_id="lead_actionable_brief"):
    return {"id": case_id, "topic_id": "lead", "snapshot_id": "SNAP-2",
            "expected": ["SECRET_EXPECTATION_ONLY_FOR_JUDGE"], "forbidden": ["invented capability"]}


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runner = FakeRunner(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_actor_approval_does_not_override_missing_acceptance_expectation(self):
        self.runner.judge.update(meets_expectations=False, missed=["no usable draft"])
        result = evaluate(self.runner, {"cases": [normal_case()]})
        self.assertEqual(result["passed_cases"], 0)
        self.assertFalse(result["initial_target"]["met"])
        self.assertEqual(result["cases"][0]["judge"]["missed"], ["no usable draft"])
        self.assertEqual(self.runner.run_inputs, [("lead", "SNAP-2", None)])
        self.assertEqual(self.runner.paid_calls[0][1], "evaluate")
        self.assertIn("acceptance_case", self.runner.paid_calls[0][2])
        self.assertEqual(result["usage"]["assignment_cost_usd"], .018)

    def test_judge_approval_cannot_override_wrong_decision_or_incomplete_run(self):
        for decision, status in [("observe", "completed"), ("recommend", "review_required")]:
            with self.subTest(decision=decision, status=status), tempfile.TemporaryDirectory() as folder:
                runner = FakeRunner(folder)
                runner.result.update(decision=decision, status=status)
                runner.result["plan"]["decision"] = decision
                report = evaluate(runner, {"cases": [normal_case()]})
                self.assertFalse(report["cases"][0]["passed"])
                self.assertEqual(report["passed_cases"], 0)

    def test_forbidden_literal_claim_fails_even_when_judge_boolean_is_positive(self):
        self.runner.judge["forbidden_claims"] = ["Example asserts an unverified feature is provided."]
        result = evaluate(self.runner, {"cases": [normal_case()]})
        self.assertFalse(result["cases"][0]["passed"])
        self.assertEqual(result["forbidden_claims_found"], 1)
        self.assertFalse(result["initial_target"]["met"])

    def test_completed_case_reuses_saved_output_without_more_calls(self):
        cases = {"cases": [normal_case()]}
        first = evaluate(self.runner, cases)
        self.runner.error = ProviderError("should not call")
        second = evaluate(self.runner, cases)
        self.assertTrue(second["cases"][0]["passed"])
        self.assertTrue(second["cases"][0]["evaluation_cache_hit"])
        self.assertEqual(len(self.runner.paid_calls), 1)
        self.assertEqual(second["usage"]["new_calls_this_invocation"], 0)
        self.assertEqual(first["suite_id"], second["suite_id"])
        saved = json.loads(Path(second["cases"][0]["artifact_path"]).read_text())
        self.assertEqual(saved["result"]["plan"], self.runner.result["plan"])

    def test_nonexistent_reference_is_checked_without_any_model_call(self):
        case = {"id": "reject_nonexistent_evidence", "topic_id": "lead", "snapshot_id": "SNAP-2",
                "stage": "validate_evidence_refs", "candidate_override": {"observation_ids": ["FAKE_ID"]}}
        result = evaluate(self.runner, {"cases": [case]})
        self.assertTrue(result["cases"][0]["passed"])
        self.assertIn("FAKE_ID", result["cases"][0]["validation_errors"][0])
        self.assertEqual(self.runner.paid_calls, [])
        self.assertEqual(result["usage"]["calls"], 0)

    def test_context_fixture_cache_key_change_is_checked_in_code(self):
        case = normal_case("lead_capability_confirmed")
        case["context_override"] = {"origin": "synthetic_eval_fixture", "verified_web_form_auto_registration": True}
        report = evaluate(self.runner, {"cases": [case]})
        self.assertTrue(report["cases"][0]["gates"]["changed_context_changes_cache_key"])
        checks = self.runner.paid_calls[0][2]["execution_checks"]["context_cache_keys"]
        self.assertNotEqual(checks["without_override"], checks["with_override"])
        self.assertEqual(self.runner.run_inputs[0][2], case["context_override"])

    def test_provider_failure_is_not_pass_and_stops_more_paid_cases(self):
        self.runner.error = ProviderError("credential-like-secret-must-not-be-logged")
        result = evaluate(self.runner, {"cases": [normal_case(), normal_case("another_case")]})
        self.assertEqual(result["suite_state"], "incomplete")
        self.assertEqual(result["execution_failures"], 1)
        self.assertEqual(result["not_run_cases"], 1)
        self.assertEqual(result["passed_cases"], 0)
        self.assertEqual(len(self.runner.paid_calls), 1)
        self.assertEqual(result["usage"]["assignment_cost_usd"], .018)
        self.assertNotIn("credential-like-secret", json.dumps(result))

    def test_rejection_test_requires_the_review_to_actually_fail(self):
        report = evaluate(self.runner, {"cases": [generic_case()]})
        self.assertTrue(report["cases"][0]["passed"])
        self.assertEqual([call[1] for call in self.runner.paid_calls], ["review", "evaluate"])
        self.assertEqual(self.runner.paid_calls[0][0], self.runner.paid_calls[1][0])
        self.assertEqual(report["usage"]["assignment_cost_usd"], .036)
        self.assertNotIn("acceptance_case", self.runner.paid_calls[0][2])
        self.assertEqual(self.runner.paid_calls[1][2]["acceptance_case"]["expected"], generic_case()["expected"])
        self.assertFalse(self.runner.run_inputs)

    def test_unrelated_rejection_does_not_pass_acceptance(self):
        self.runner.review_response["issues"] = [
            {"path": "title", "problem": "Typography", "required_change": "Change heading size"}
        ]
        self.runner.judge.update(meets_expectations=False,
                                 missed=["Reviewer did not identify missing deliverables or measurement"])
        report = evaluate(self.runner, {"cases": [generic_case()]})
        self.assertFalse(report["cases"][0]["passed"])
        self.assertTrue(report["cases"][0]["gates"]["candidate_rejected"])
        self.assertEqual(report["passed_cases"], 0)

    def test_bad_candidate_approval_cannot_pass_even_with_positive_evaluation(self):
        self.runner.review_response.update(verdict="approve", issues=[])
        for score in self.runner.review_response["scores"].values():
            score["value"] = 2
        report = evaluate(self.runner, {"cases": [generic_case()]})
        self.assertFalse(report["cases"][0]["passed"])
        self.assertFalse(report["cases"][0]["gates"]["candidate_rejected"])

    def test_review_validation_failure_cannot_pass_a_rejection_test(self):
        self.runner.review_response["claim_checks"] = [{
            "quote": "OrbitCRM provides native auto-registration", "subject": "OrbitCRM",
            "status": "verified", "asserted_as_fact": True, "verification_ref": None,
        }]
        report = evaluate(self.runner, {"cases": [generic_case()]})
        self.assertFalse(report["cases"][0]["passed"])
        self.assertTrue(report["cases"][0]["review_validation_errors"])
        self.assertFalse(report["cases"][0]["gates"]["review_validation_passed"])


if __name__ == "__main__":
    unittest.main()
