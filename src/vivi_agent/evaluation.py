"""실제 생성 결과의 요구사항 충족 여부를 평가한다. 모델 평가가 사업 효과를 입증하지는 않는다."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .graph import digest, prompt_evidence
from .ledger import BudgetExceeded, InFlightError
from .models import CaseEvaluation, Review
from .provider import ProviderError
from .validation import validate_references, validate_review


VERSION = "2026-10-14.v3"
EXPECTED_DECISIONS = {
    "lead_actionable_brief": ["recommend"],
    "automation_observe": ["observe"],
    "scale_missing_observations": ["needs_data"],
    "security_existing_asset": ["recommend", "needs_data"],
    "lead_capability_unsupported": ["recommend", "needs_data"],
    "lead_capability_confirmed": ["recommend"],
}


def _write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _usage(runner, keys):
    by_key = {key: runner.ledger.stats(key) for key in sorted(set(keys))}
    return {
        "by_run_key": by_key,
        "calls": sum(row["calls"] for row in by_key.values()),
        "attempted_calls": sum(row.get("attempted_calls", row["calls"]) for row in by_key.values()),
        "assignment_cost_usd": round(sum(row["charged_cost_usd"] for row in by_key.values()), 6),
        "actual_vendor_cost_usd": None,
        "cost_note": "AI-M assignment cost includes attempted/uncertain calls; vendor billing is not inferred.",
    }


def _expected_decisions(case):
    if case.get("expected_decision"):
        return [case["expected_decision"]]
    return EXPECTED_DECISIONS.get(case["id"], ["recommend", "observe", "needs_data"])


def evaluate(runner, cases: dict) -> dict:
    """기대 결과를 생성 그래프에 노출하지 않고 평가 케이스를 실행한다.

    유료 평가는 별도 평가 키로 기존 원장을 거치며, 생성과 월 비용·호출 예산을 공유한다.
    완료된 결과와 영수증은 재시작 후에도 재사용한다.
    """
    specifications = cases["cases"]
    if len({case["id"] for case in specifications}) != len(specifications):
        raise ValueError("Evaluation case IDs must be unique")
    prepared = []
    for case in specifications:
        pack = runner.repository.build(case["topic_id"], case.get("snapshot_id", "SNAP-2"),
                                       case.get("context_override"))
        prepared.append((case, pack, runner.key(pack)))
    suite_key = digest({
        "version": VERSION, "implementation": Path(__file__).read_text(),
        "cases": cases, "generation_keys": [item[2] for item in prepared],
        "model": runner.provider.model,
        "provider": getattr(runner.provider, "cache_identity", type(runner.provider).__name__),
    })
    directory = Path(runner.state_dir) / "evaluations" / suite_key
    directory.mkdir(parents=True, exist_ok=True)
    keys = set()
    for case, _, generation_key in prepared:
        if not case.get("stage"):
            keys.add(generation_key)
        if case.get("stage") != "validate_evidence_refs":
            keys.add(digest({"suite": suite_key, "case": case["id"], "kind": "acceptance_evaluation"}))
    before = _usage(runner, keys)
    report = {
        "status": "live_model_evaluation_not_human",
        "suite_id": suite_key,
        "suite_state": "running",
        "model": runner.provider.model,
        "evaluation_version": VERSION,
        "artifact_directory": str(directory),
        "cases": [],
        "limitations": [
            "Separate judge calls use the same provider/model and can share its errors.",
            "Passing the acceptance suite is not evidence of human adoption or business impact.",
            "Synthetic capability fixtures test behavior, not facts about the real product.",
            "Review original deliverable examples: future verification prerequisites do not excuse unsupported factual claims in drafts.",
        ],
    }
    stopped = None

    def save_progress():
        report["total_cases"] = len(specifications)
        report["evaluated_cases"] = sum(row["status"] == "evaluated" for row in report["cases"])
        report["passed_cases"] = sum(row["passed"] for row in report["cases"])
        report["failed_cases"] = sum(row["status"] == "evaluated" and not row["passed"]
                                     for row in report["cases"])
        report["execution_failures"] = sum(row["status"] == "execution_failed" for row in report["cases"])
        report["not_run_cases"] = sum(row["status"] == "not_run" for row in report["cases"])
        report["forbidden_claims_found"] = sum(
            len(row.get("judge", {}).get("forbidden_claims", [])) for row in report["cases"]
        )
        report["final_product_hard_violations"] = sum(
            len((row.get("result", {}).get("review") or {}).get("hard_violations", []))
            + sum(check.get("status") == "unverified" and check.get("asserted_as_fact", False)
                  for check in (row.get("result", {}).get("review") or {}).get("claim_checks", []))
            for row in report["cases"]
        )
        target = cases.get("initial_target", {})
        report["initial_target"] = {
            "required_passes": target.get("cases_meeting_expectations", len(specifications)),
            "allowed_hard_violations": target.get("allowed_hard_violations", 0),
            "met": (report["evaluated_cases"] == len(specifications)
                    and report["passed_cases"] >= target.get("cases_meeting_expectations", len(specifications))
                    and report["forbidden_claims_found"] + report["final_product_hard_violations"]
                    <= target.get("allowed_hard_violations", 0)),
        }
        usage = _usage(runner, keys)
        usage["new_calls_this_invocation"] = usage["calls"] - before["calls"]
        usage["new_assignment_cost_usd"] = round(usage["assignment_cost_usd"] - before["assignment_cost_usd"], 6)
        report["usage"] = usage
        _write(directory / "report.json", report)

    save_progress()
    for case, pack, generation_key in prepared:
        slug = re.sub(r"[^a-zA-Z0-9_-]", "-", case["id"])[:80] or "case"
        case_directory = directory / f"{slug}-{digest(case)[:8]}"
        artifact_path = case_directory / "result.json"
        evaluation_key = digest({"suite": suite_key, "case": case["id"], "kind": "acceptance_evaluation"})
        if artifact_path.exists():
            cached = json.loads(artifact_path.read_text(encoding="utf-8"))
            if cached.get("status") == "evaluated":
                cached["evaluation_cache_hit"] = True
                report["cases"].append(cached)
                save_progress()
                continue
        row = {
            "id": case["id"], "topic_id": case["topic_id"],
            "snapshot_id": case.get("snapshot_id", "SNAP-2"),
            "status": "running", "passed": False,
            "generation_key": generation_key, "evaluation_key": evaluation_key,
            "artifact_path": str(artifact_path), "evaluation_cache_hit": False,
            "expected": case.get("expected", []), "forbidden": case.get("forbidden", []),
        }
        stage = case.get("stage")
        if stopped and stage != "validate_evidence_refs":
            row.update(status="not_run", failure={"error_type": stopped,
                       "reason": "Further paid evaluation stopped after a runtime/provider failure."})
        else:
            _write(case_directory / "evidence.json", pack)
            try:
                if stage == "validate_evidence_refs":
                    candidate = case["candidate_override"]
                    errors = validate_references(candidate, pack)
                    row.update(status="evaluated", evaluation_kind="deterministic_reference_check",
                               candidate=candidate, validation_errors=errors, passed=bool(errors))
                elif stage == "review_actions":
                    candidate = case["candidate_override"]
                    review = runner.call(evaluation_key, "review", Review,
                                         {"evidence": prompt_evidence(pack), "candidate": candidate})
                    errors = validate_review(review, pack)
                    review_passed = Review.model_validate(review).passes()
                    row.update(candidate=candidate, review=review, review_validation_errors=errors)
                    _write(case_directory / "candidate-review.json", row)
                    judge = runner.call(evaluation_key, "evaluate", CaseEvaluation, {
                        "evidence": prompt_evidence(pack),
                        "actual_result": {
                            "candidate": candidate, "review": review,
                            "review_passed": review_passed, "review_validation_errors": errors,
                        },
                        "acceptance_case": {
                            "id": case["id"], "stage": stage,
                            "expected": case.get("expected", []),
                            "forbidden": case.get("forbidden", []),
                        },
                        "evaluation_instruction": (
                            "This is a deliberately bad-candidate rejection test. Evaluate the reviewer's "
                            "actual reasons and required changes against every case expectation. "
                            "A rejection for unrelated reasons does not pass. Valid evidence IDs and "
                            "JSON alone cannot justify acceptance. Check whether missing problem linkage, "
                            "usable deliverables, execution conditions and measurement are identified "
                            "when required by this case. Do not credit failures that the reviewer did not identify. "
                            "Forbidden claims refers to mistakes made by the reviewer, not defects deliberately "
                            "planted in the candidate and correctly rejected."
                        ),
                    })
                    parsed = CaseEvaluation.model_validate(judge)
                    row.update(status="evaluated", evaluation_kind="model_rejection_test",
                               judge=judge,
                               gates={"candidate_rejected": not review_passed,
                                      "review_validation_passed": not errors},
                               passed=(not review_passed and not errors and parsed.meets_expectations
                                       and not parsed.missed and not parsed.forbidden_claims))
                elif stage is None:
                    result = runner.run(case["topic_id"], case.get("snapshot_id", "SNAP-2"),
                                        case.get("context_override"))
                    row["result"] = result
                    _write(case_directory / "agent-result.json", result)
                    expected_decisions = _expected_decisions(case)
                    actual_decision = result.get("decision")
                    plan_decision = (result.get("plan") or {}).get("decision")
                    gates = {
                        "run_completed": result.get("status") == "completed",
                        "expected_decision": actual_decision in expected_decisions,
                        "plan_agrees_with_result": plan_decision == actual_decision,
                        "no_validation_errors": not result.get("validation_errors", []),
                    }
                    execution_checks = {}
                    if case.get("context_override"):
                        base_pack = runner.repository.build(case["topic_id"], case.get("snapshot_id", "SNAP-2"))
                        base_key = runner.key(base_pack)
                        gates["changed_context_changes_cache_key"] = generation_key != base_key
                        execution_checks["context_cache_keys"] = {
                            "without_override": base_key, "with_override": generation_key,
                            "changed": generation_key != base_key,
                        }
                    judge = runner.call(evaluation_key, "evaluate", CaseEvaluation, {
                        "evidence": prompt_evidence(pack),
                        # 재실행 시 캐시·비용 메타데이터만 달라지며 평가 대상 결과는 같다.
                        "actual_result": {name: result.get(name) for name in
                                          ("status", "decision", "diagnosis", "plan", "execution_contracts",
                                           "review", "validation_errors")},
                        "execution_checks": execution_checks,
                        "acceptance_case": {
                            "id": case["id"], "expected": case.get("expected", []),
                            "forbidden": case.get("forbidden", []),
                            "allowed_decisions": expected_decisions,
                        },
                        "evaluation_instruction": (
                            "Inspect literal claims inside every example/table/draft, including competitor claims. "
                            "A future fact-check or publishing prerequisite does not make an unverified statement true. "
                            "Verify any numerical claim against evidence metrics. Ignore the candidate's review score. "
                            "Execution contracts are computed from metric denominators and define primary/secondary "
                            "sampling; check that the narrative and success criteria agree with them. "
                            "Verified-route procedures require concrete test inputs, operations, expected observations "
                            "and recorded evidence. Do not require or credit invented UI paths or API endpoints. "
                            "Existing acceptance expectations and forbidden claims still apply unchanged."
                        ),
                    })
                    parsed = CaseEvaluation.model_validate(judge)
                    row.update(status="evaluated", evaluation_kind="separate_model_acceptance_judge",
                               gates=gates, execution_checks=execution_checks,
                               expected_decisions=expected_decisions, judge=judge,
                               passed=(all(gates.values()) and parsed.meets_expectations
                                       and not parsed.missed and not parsed.forbidden_claims))
                else:
                    raise ValueError("Unsupported evaluation stage")
            except Exception as exc:
                # 공급자 예외 원문에는 인증 정보가 있을 수 있어 저장하지 않는다.
                failure = {"error_type": type(exc).__name__}
                if isinstance(exc, ProviderError):
                    receipt = getattr(runner.provider, "last_receipt", {})
                    failure.update({name: receipt[name] for name in
                                    ("error_type", "http_status", "request_id", "trace_path")
                                    if receipt.get(name) is not None})
                    failure["exception_type"] = type(exc).__name__
                row.update(status="execution_failed", passed=False, failure=failure)
                if isinstance(exc, (ProviderError, BudgetExceeded, InFlightError)):
                    stopped = type(exc).__name__
        row["usage"] = {
            "generation": runner.ledger.stats(generation_key) if stage is None else None,
            "evaluation": runner.ledger.stats(evaluation_key) if stage != "validate_evidence_refs" else None,
        }
        _write(artifact_path, row)
        report["cases"].append(row)
        save_progress()
    report["suite_state"] = "completed" if report["evaluated_cases"] == len(specifications) else "incomplete"
    save_progress()
    return report
