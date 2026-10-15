"""모델 호출이나 주제별 규칙 없이 보수적인 데이터 충족 정책을 검증한다."""

import pytest

from vivi_agent.validation import validate_diagnosis, validate_plan


def evidence_pack(eligible=7):
    questions = [{"question_id": f"Q{i}"} for i in range(1, 6)]
    channels = ["ChatGPT", "Gemini"]
    rows = [
        {"observation_id": f"OBS{i}", "question_id": question["question_id"],
         "channel": channel, "citations": []}
        for i, (question, channel) in enumerate(
            (question, channel) for question in questions for channel in channels
        )
    ]
    excluded = [
        {**row, "reason": ["failed", "partial", "language_mismatch"][i % 3]}
        for i, row in enumerate(rows[eligible:])
    ]
    return {
        "scope": {"topic_id": "arbitrary_topic", "target_brand_id": "BR001"},
        "quality": {"eligible": eligible, "expected": 10, "coverage": eligible / 10,
                    "by_channel": {channel: {} for channel in channels}},
        "questions": questions,
        "sources": [],
        "observations": rows[:eligible],
        "excluded": excluded,
        "metrics": {"topic.target_mentions": {"numerator": 0, "denominator": eligible}},
        "allowed_evidence_ids": [row["observation_id"] for row in rows],
    }


def diagnosis(decision):
    return {
        "decision": decision,
        "opportunity": "Inspect the observed scope with its collection coverage.",
        "facts": [{"statement": "The metric is computed from eligible observations.",
                   "evidence_ids": [], "metric_ids": ["topic.target_mentions"]}],
        "hypotheses": [], "evidence_requests": [], "limitations": [],
    }


def recollection_tasks(pack):
    return [
        {"question_id": row["question_id"], "channel": row["channel"],
         "reason": row["reason"],
         "completion_condition": "A complete response in the requested language is collected."}
        for row in pack["excluded"] if row["reason"] != "superseded"
    ]


def plan(decision, tasks=None):
    action = {
        "title": "Prepare an evidence-linked workflow checklist",
        "action_type": "content", "question_ids": ["Q1"], "channels": ["ChatGPT"],
        "evidence_ids": ["OBS0"], "opportunity": "Address an observed question.",
        "mechanism_hypothesis": "A clearer explanation may help; the effect is unverified.",
        "target_asset": {"mode": "create", "source_ids": [], "proposed_title": "Workflow checklist",
                         "selection_rule": "Use verified product capabilities only.", "assessments": []},
        "owner_role": "Content owner",
        "deliverables": [{"name": "Checklist", "outline": ["Inputs", "Results"],
                          "example_content": "Input field | Verified supported path | Expected result | Failure handling"}],
        "steps": [{"instruction": "Record the inputs.", "expected_output": "Input table"},
                  {"instruction": "Document observed results.", "expected_output": "Result table"}],
        "prerequisites": [],
        "acceptance_criteria": ["Inputs are recorded.", "Results can be reproduced."],
        "measurement": {"baseline_metric_ids": ["topic.target_mentions"],
                        "secondary_metric_ids": [],
                        "follow_up": "Repeat the same questions.", "positive_signal": "New supported mention.",
                        "stop_or_revise": "Revise after unchanged observations."},
        "priority_reason": "Observed question gap.", "effort_assumptions": "One content owner.",
        "readiness": "ready_to_start", "integration_procedure": None,
    }
    return {
        "decision": decision, "summary": "Collect evidence before choosing follow-up work.",
        "actions": [action] if decision == "recommend" else [],
        "rejected_alternatives": [], "monitoring_plan": "Repeat the same scoped questions.",
        "data_tasks": tasks or [], "limitations": [],
    }


@pytest.mark.parametrize("decision", ["recommend", "observe"])
def test_low_coverage_diagnosis_requires_needs_data(decision):
    errors = validate_diagnosis(diagnosis(decision), evidence_pack())
    assert any("diagnosis.decision" in error and "requires needs_data" in error for error in errors)


def test_low_coverage_diagnosis_accepts_needs_data():
    assert validate_diagnosis(diagnosis("needs_data"), evidence_pack()) == []


@pytest.mark.parametrize("decision", ["recommend", "observe"])
def test_low_coverage_plan_rejects_other_decisions_even_with_complete_recollection(decision):
    pack = evidence_pack()
    errors = validate_plan(plan(decision, recollection_tasks(pack)), pack)
    assert any("plan.decision" in error and "requires needs_data" in error for error in errors)


def test_low_coverage_needs_data_covers_every_excluded_pair():
    pack = evidence_pack()
    tasks = recollection_tasks(pack)
    assert validate_plan(plan("needs_data", tasks), pack) == []
    omitted = tasks.pop()
    errors = validate_plan(plan("needs_data", tasks), pack)
    assert any(
        f"requires recollection of {omitted['question_id']}/{omitted['channel']}" in error
        for error in errors
    )


def test_low_coverage_empty_tasks_reports_each_missing_pair():
    pack = evidence_pack()
    errors = validate_plan(plan("needs_data"), pack)
    for row in pack["excluded"]:
        assert any(f"requires recollection of {row['question_id']}/{row['channel']}" in error for error in errors)


def test_superseded_attempt_does_not_need_recollection():
    pack = evidence_pack()
    duplicate = {**pack["observations"][0], "observation_id": "OBS_DUP", "reason": "superseded"}
    pack["excluded"].append(duplicate)
    pack["allowed_evidence_ids"].append("OBS_DUP")
    assert validate_plan(plan("needs_data", recollection_tasks(pack)), pack) == []


@pytest.mark.parametrize("eligible", [8, 9, 10])
@pytest.mark.parametrize("decision", ["recommend", "observe"])
def test_at_or_above_threshold_preserves_existing_decision_behavior(eligible, decision):
    pack = evidence_pack(eligible)
    assert validate_diagnosis(diagnosis(decision), pack) == []
    assert validate_plan(plan(decision), pack) == []


def test_at_threshold_does_not_require_all_recollection_tasks():
    pack = evidence_pack(8)
    assert validate_plan(plan("needs_data", recollection_tasks(pack)[:1]), pack) == []


def test_unexcluded_pair_remains_invalid_recollection_target():
    pack = evidence_pack()
    tasks = recollection_tasks(pack)
    tasks.append({"question_id": "Q1", "channel": "ChatGPT", "reason": "Already valid",
                  "completion_condition": "Collect again"})
    errors = validate_plan(plan("needs_data", tasks), pack)
    assert any("Q1/ChatGPT is not an excluded pair" in error for error in errors)
