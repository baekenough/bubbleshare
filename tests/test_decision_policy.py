"""주제와 무관한 초기 판단 정책과 단일 액션 제약을 검증한다."""

from copy import deepcopy

import pytest

from vivi_agent.validation import allowed_decisions, validate_diagnosis, validate_plan


def pack(coverage=1.0, numerator=4, denominator=5):
    rows = [
        {"observation_id": f"OBS{i}", "question_id": f"Q{i}", "channel": "ChatGPT", "citations": []}
        for i in range(1, 6)
    ]
    eligible = int(coverage * len(rows))
    return {
        "scope": {"topic_id": "arbitrary_scope", "target_brand_id": "BR001"},
        "quality": {"coverage": coverage, "eligible": eligible, "expected": 5,
                    "by_channel": {"ChatGPT": {}}},
        "metrics": {"topic.target_recommendations": {"numerator": numerator, "denominator": denominator}},
        "questions": [{"question_id": row["question_id"]} for row in rows],
        "sources": [], "allowed_evidence_ids": [row["observation_id"] for row in rows],
        "observations": rows[:eligible],
        "excluded": [{**row, "reason": "partial"} for row in rows[eligible:]],
    }


def diagnosis(decision):
    return {
        "decision": decision, "opportunity": "Consider the observed recommendation coverage.",
        "facts": [{"statement": "Recommendation coverage comes from eligible responses.",
                   "metric_ids": ["topic.target_recommendations"], "evidence_ids": []}],
        "hypotheses": [], "evidence_requests": [], "limitations": [],
    }


def action():
    return {
        "title": "Prepare a verified workflow checklist", "action_type": "content",
        "question_ids": ["Q1"], "channels": ["ChatGPT"], "evidence_ids": ["OBS1"],
        "opportunity": "Address an observed gap.",
        "mechanism_hypothesis": "A clear answer may help; no effect is established.",
        "target_asset": {"mode": "create", "source_ids": [], "proposed_title": "Checklist",
                         "selection_rule": "Use verified instructions only.", "assessments": []},
        "owner_role": "Content owner",
        "deliverables": [{"name": "Checklist", "outline": ["Input", "Result"],
                          "example_content": "Input field | Verified supported path | Expected result | Failure handling"}],
        "steps": [{"instruction": "Record inputs.", "expected_output": "Input table"},
                  {"instruction": "Record outcomes.", "expected_output": "Outcome table"}],
        "prerequisites": [], "acceptance_criteria": ["Inputs documented.", "Outcomes reproducible."],
        "measurement": {"baseline_metric_ids": ["topic.target_recommendations"],
                        "secondary_metric_ids": [],
                        "follow_up": "Repeat the same scoped questions.", "positive_signal": "New recommendation.",
                        "stop_or_revise": "Revisit unchanged results."},
        "priority_reason": "Observed gap.", "effort_assumptions": "One content owner.",
        "readiness": "ready_to_start", "integration_procedure": None,
    }


def plan(decision, actions=None, tasks=None):
    return {
        "decision": decision, "summary": "Select work according to current evidence.",
        "actions": actions or [], "rejected_alternatives": [],
        "monitoring_plan": "Repeat the same scope at the next scheduled collection.",
        "data_tasks": tasks or [], "limitations": [],
    }


@pytest.mark.parametrize("coverage,numerator,denominator,expected", [
    (0.79, 100, 100, ["needs_data"]),
    (0.8, 4, 4, ["observe"]),
    (1.0, 5, 5, ["observe"]),
    (1.0, 0, 0, ["recommend", "observe", "needs_data"]),
    (1.0, 4, 5, ["recommend", "observe", "needs_data"]),
    (1.0, 0, 5, ["recommend", "observe", "needs_data"]),
])
def test_allowed_decisions_follow_quality_then_recommendation_coverage(coverage, numerator, denominator, expected):
    assert allowed_decisions(pack(coverage, numerator, denominator)) == expected


def test_missing_recommendation_metric_does_not_manufacture_full_coverage():
    evidence = pack()
    evidence["metrics"] = {}
    assert allowed_decisions(evidence) == ["recommend", "observe", "needs_data"]


@pytest.mark.parametrize("decision", ["recommend", "needs_data"])
def test_complete_recommendation_coverage_rejects_other_diagnosis_and_plan_decisions(decision):
    evidence = pack(numerator=5)
    assert any("requires observe" in error for error in validate_diagnosis(diagnosis(decision), evidence))
    result = plan(decision, actions=[action()] if decision == "recommend" else [])
    assert any("requires observe" in error for error in validate_plan(result, evidence))


def test_complete_recommendation_coverage_accepts_observe_without_actions():
    evidence = pack(numerator=5)
    assert validate_diagnosis(diagnosis("observe"), evidence) == []
    assert validate_plan(plan("observe"), evidence) == []
    assert any("observe requires no actions" in error
               for error in validate_plan(plan("observe", actions=[action()]), evidence))


def test_data_quality_precedence_keeps_all_missing_pair_recollection():
    evidence = pack(coverage=0.6, numerator=3, denominator=3)
    assert allowed_decisions(evidence) == ["needs_data"]
    assert validate_diagnosis(diagnosis("needs_data"), evidence) == []
    tasks = [{"question_id": row["question_id"], "channel": row["channel"], "reason": "partial",
              "completion_condition": "Complete response in the requested language."}
             for row in evidence["excluded"]]
    assert validate_plan(plan("needs_data", tasks=tasks), evidence) == []
    assert any("requires recollection of Q5/ChatGPT" in error
               for error in validate_plan(plan("needs_data", tasks=tasks[:1]), evidence))


def test_recommendation_prioritizes_only_one_action():
    evidence = pack(numerator=4)
    candidate = action()
    assert validate_plan(plan("recommend", actions=[candidate]), evidence) == []
    errors = validate_plan(plan("recommend", actions=[candidate, deepcopy(candidate)]), evidence)
    assert any("actions" in error and ("at most one" in error or "at most 1" in error)
               for error in errors)


def test_policy_uses_metrics_not_topic_name():
    evidence = pack(numerator=5)
    before = deepcopy(evidence)
    evidence["scope"]["topic_id"] = "unseen_business_topic"
    assert allowed_decisions(evidence) == allowed_decisions(before) == ["observe"]
