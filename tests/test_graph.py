from copy import deepcopy
import json
from pathlib import Path

import pytest

from vivi_agent.evidence import EvidenceRepository
from vivi_agent.graph import AgentRunner
from vivi_agent.ledger import BudgetExceeded
from vivi_agent.models import Review
from vivi_agent.provider import ProviderError
from vivi_agent.validation import validate_references

ROOT = Path(__file__).resolve().parents[1]


def diagnosis():
    return {"decision": "observe", "opportunity": "현재 추천 유지",
            "facts": [{"statement": "추천이 관측됨", "evidence_ids": [],
                       "metric_ids": ["topic.target_recommendations"]}],
            "hypotheses": [], "evidence_requests": [], "limitations": []}


def plan():
    return {"decision": "observe", "summary": "유지 관찰", "actions": [],
            "rejected_alternatives": [], "monitoring_plan": "다음 주 같은 질문에서 추천 감소 시 재검토",
            "data_tasks": [], "limitations": ["인과 효과는 측정하지 않음"]}


def review(approve=True):
    return {"verdict": "approve" if approve else "revise",
            "scores": {k: {"value": 2, "reason": "근거와 일치"}
                       for k in ("grounding", "relevance", "specificity", "feasibility", "verification")},
            "hard_violations": [], "issues": [] if approve else [
                {"path": "monitoring_plan", "problem": "조건 미흡", "required_change": "구체화"}],
            "claim_checks": [], "rationale": "모델 검토 테스트 대역"}


class ScriptedProvider:
    model = "test-double-not-live"
    last_receipt = {"test_double": True}

    def __init__(self, values):
        self.values, self.stages = values, []

    def generate(self, stage, prompt, schema):
        self.stages.append(stage)
        value = self.values.pop(0)
        if isinstance(value, Exception): raise value
        return deepcopy(value)


def runner(tmp_path, values):
    provider = ScriptedProvider(values)
    return AgentRunner(EvidenceRepository(ROOT / "VIVI_MVP_Sample_Dataset"), provider, tmp_path), provider


def test_real_graph_persists_and_reuses_completed_result(tmp_path):
    agent, provider = runner(tmp_path, [diagnosis(), plan(), review()])
    first = agent.run("automation")
    assert first["decision"] == "observe" and first["usage"]["calls"] == 3
    # 새 런타임 인스턴스로 메모리 캐시의 영향을 배제한다.
    restarted, empty = runner(tmp_path, [])
    second = restarted.run("automation")
    assert second["cache_hit"] is True and not empty.stages
    assert first["run_id"] == second["run_id"]


def test_revised_output_is_reviewed_again_and_capped_at_five(tmp_path):
    agent, provider = runner(tmp_path, [diagnosis(), plan(), review(False), plan(), review()])
    result = agent.run("automation")
    assert result["status"] == "completed"
    assert provider.stages == ["diagnose", "design", "review", "revise", "re_review"]
    assert result["usage"]["calls"] == 5


def test_rejected_revision_never_published(tmp_path):
    agent, _ = runner(tmp_path, [diagnosis(), plan(), review(False), plan(), review(False)])
    result = agent.run("automation")
    assert result["decision"] == "review_required"
    assert result["usage"]["calls"] == 5


def test_resume_failure_reuses_prior_paid_stage(tmp_path):
    agent, provider = runner(tmp_path, [diagnosis(), ProviderError("unavailable"), plan(), review()])
    with pytest.raises(ProviderError): agent.run("automation")
    result = agent.run("automation")
    assert result["status"] == "completed" and result["usage"]["calls"] == 4
    assert provider.stages.count("diagnose") == 1
    assert result["usage"]["failed"] == 1


@pytest.mark.parametrize("stage,calls,released", [("revise", 4, 1), ("re_review", 5, 0)])
def test_failed_repair_is_terminal_and_does_not_rebind_or_retry(tmp_path, stage, calls, released):
    values = [diagnosis(), plan(), review(False)]
    if stage == "re_review":
        values.append(plan())
    values.append(ProviderError("secret-error-sentinel"))
    agent, provider = runner(tmp_path, values)
    result = agent.run("automation")
    assert result["status"] == "review_required"
    assert result["execution_error"] == {"stage": stage, "error_type": "ProviderError"}
    assert "secret-error-sentinel" not in json.dumps(result)
    assert result["usage"]["calls"] == calls
    assert result["usage"]["failed"] == 1
    assert result["usage"]["reserved"] == 0
    assert result["usage"]["released"] == released
    assert len(provider.stages) == calls
    restarted, empty = runner(tmp_path, [])
    cached = restarted.run("automation")
    assert cached["status"] == "review_required" and cached["cache_hit"]
    assert not empty.stages


def test_repair_reservation_budget_race_returns_review_required(tmp_path, monkeypatch):
    agent, provider = runner(tmp_path, [diagnosis(), plan(), review(False)])

    def unavailable(_):
        raise BudgetExceeded("secret-budget-sentinel")

    monkeypatch.setattr(agent.ledger, "reserve_pair", unavailable)
    result = agent.run("automation")
    assert result["status"] == "review_required"
    assert result["execution_error"] == {"stage": "reserve_repair", "error_type": "BudgetExceeded"}
    assert result["usage"]["calls"] == 3 and result["usage"]["reserved"] == 0
    assert provider.stages == ["diagnose", "design", "review"]
    assert "secret-budget-sentinel" not in json.dumps(result)


def test_review_reference_errors_override_model_approval_and_require_new_review(tmp_path, monkeypatch):
    errors = iter([["claim_checks[0]: no verified business fact"], []])
    monkeypatch.setattr("vivi_agent.graph.validate_review", lambda *_: next(errors))
    agent, provider = runner(tmp_path, [diagnosis(), plan(), review(), plan(), review()])
    result = agent.run("automation")
    assert result["status"] == "completed" and not result["review_errors"]
    assert provider.stages == ["diagnose", "design", "review", "revise", "re_review"]


def test_re_review_reference_errors_fail_closed_even_with_approve(tmp_path, monkeypatch):
    monkeypatch.setattr("vivi_agent.graph.validate_review", lambda *_: ["unverified claimed proof"])
    agent, provider = runner(tmp_path, [diagnosis(), plan(), review(), plan(), review()])
    result = agent.run("automation")
    assert result["status"] == "review_required"
    assert result["review_errors"] == ["unverified claimed proof"]
    assert result["usage"]["calls"] == 5
    assert provider.stages[-1] == "re_review"


def test_valid_schema_does_not_override_unsafe_claim(tmp_path):
    r = review()
    r["claim_checks"] = [{"quote": "자동 배정 제공", "subject": "target",
                          "status": "unverified", "asserted_as_fact": True,
                          "verification_ref": None}]
    assert not Review.model_validate(r).passes()
    agent, _ = runner(tmp_path, [diagnosis(), plan(), r, plan(), r])
    result = agent.run("automation")
    assert result["decision"] == "review_required"


def test_invalid_diagnosis_cannot_reach_action_generation(tmp_path):
    invalid = diagnosis()
    invalid["facts"][0]["evidence_ids"] = ["OBS_DOES_NOT_EXIST"]
    agent, provider = runner(tmp_path, [invalid])
    result = agent.run("automation")
    assert result["status"] == "review_required" and result["plan"] is None
    assert provider.stages == ["diagnose"]


def test_dataset_context_changes_cache_key(tmp_path):
    agent, _ = runner(tmp_path, [])
    repo = agent.repository
    assert agent.key(repo.build("lead")) != agent.key(repo.build("lead", context={"verified_web_form_auto_registration": False}))


def test_wrong_scope_evidence_rejected():
    pack = EvidenceRepository(ROOT / "VIVI_MVP_Sample_Dataset").build("lead")
    assert validate_references({"evidence_ids": ["OBS0001"]}, pack)
