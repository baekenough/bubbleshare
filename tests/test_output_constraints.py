from pathlib import Path

from vivi_agent.evidence import EvidenceRepository
from vivi_agent.graph import constrain_schema, prompt_evidence
from vivi_agent.models import ActionPlan, Diagnosis

REPO = EvidenceRepository(Path(__file__).resolve().parents[1] / "VIVI_MVP_Sample_Dataset")


def test_schema_prevents_unowned_asset_selection_before_paid_generation():
    evidence = prompt_evidence(REPO.build("lead"))
    schema = constrain_schema(ActionPlan.model_json_schema(), evidence)
    sources = schema["$defs"]["Asset"]["properties"]["source_ids"]["items"]["enum"]
    assert "SRC022" in sources
    assert "SRC024" not in sources  # OrbitCRM처럼 보이는 URL이지만 소유권은 미확인이다.
    assert "SRC025" not in sources  # 경쟁사 소유.
    assert schema["properties"]["actions"]["maxItems"] == 1


def test_schema_has_no_marketing_action_when_data_or_performance_policy_forces_other_decision():
    for topic, decision in [("automation", "observe"), ("scale", "needs_data")]:
        evidence = prompt_evidence(REPO.build(topic))
        schema = constrain_schema(ActionPlan.model_json_schema(), evidence)
        assert schema["properties"]["decision"]["enum"] == [decision]
        assert schema["properties"]["actions"]["maxItems"] == 0


def test_lookup_references_cannot_include_prose_or_invented_metrics():
    evidence = prompt_evidence(REPO.build("scale"))
    schema = constrain_schema(Diagnosis.model_json_schema(), evidence)
    requests = schema["properties"]["evidence_requests"]["items"]["enum"]
    assert "OBS0311" in requests
    assert "OBS0311: 원문을 확인하세요" not in requests
    metrics = schema["$defs"]["Fact"]["properties"]["metric_ids"]["items"]["enum"]
    assert "topic.target_mentions" in metrics
    assert "made_up_metric" not in metrics
