"""실제 그래프·데이터와 유료 단계 대역으로 검증한다. 공급사 API는 호출하지 않는다."""

from copy import deepcopy
import json
from pathlib import Path
import sqlite3

import pytest

from vivi_agent.evidence import EvidenceRepository
from vivi_agent.graph import AgentRunner
from vivi_agent.measurement import evaluate_follow_up, render_measurement


ROOT = Path(__file__).resolve().parents[1]
MEASUREMENT_FIELDS = ("follow_up", "positive_signal", "stop_or_revise")


class ScriptedProvider:
    model = "measurement-render-test-double-not-live"
    last_receipt = {"test_double": True, "actual_provider_calls": 0}

    def __init__(self, values):
        self.values = deepcopy(values)
        self.stages = []
        self.inputs = {}

    def generate(self, stage, prompt, schema):
        self.stages.append(stage)
        self.inputs[stage] = json.loads(prompt.split("\nINPUT JSON:\n", 1)[1])
        return self.values.pop(0)


def _review(approve=True):
    return {
        "verdict": "approve" if approve else "revise",
        "scores": {
            dimension: {"value": 2, "reason": "Scripted semantic review; not a quality measurement."}
            for dimension in ("grounding", "relevance", "specificity", "feasibility", "verification")
        },
        "hard_violations": [],
        "issues": [] if approve else [{
            "path": "summary", "problem": "Fixture requests a revision.",
            "required_change": "Clarify the summary while retaining the selected metrics.",
        }],
        "claim_checks": [],
        "rationale": "No live evaluator was called.",
    }


def _plan(pack, stage):
    plan = json.loads((ROOT / "output/lead.v7.json").read_text())["plan"]
    action = plan["actions"][0]
    asset = action["target_asset"]
    candidates = set(asset["source_ids"])
    asset["assessments"] = [
        {
            "source_id": source["source_id"],
            "reuse_decision": (
                "inspect_for_reuse" if source["source_id"] in candidates else "exclude_for_this_task"
            ),
            "rationale": (
                "Inspect this candidate's body for the selected task before choosing it."
                if source["source_id"] in candidates
                else "Do not edit this asset for this test's selected Q068 task."
            ),
        }
        for source in pack["sources"]
        if source["is_brand_owned"] and source["brand_id"] == pack["scope"]["target_brand_id"]
    ]
    # 범위를 넓혀 렌더링 누락을 검출한다. 주 액션의 Q068 쌍 3개를 넘어
    # 총 7쌍이 표시되어야 한다.
    action["measurement"]["secondary_metric_ids"] = ["channel.Perplexity.target_mentions"]
    for field in MEASUREMENT_FIELDS:
        action["measurement"][field] = f"RAW_INCOMPLETE_{stage}_{field}"
    plan["monitoring_plan"] = f"RAW_INCOMPLETE_{stage}_monitoring_plan"
    return plan


@pytest.mark.parametrize("requires_revision", [False, True])
def test_review_and_result_use_rendered_contract_but_paid_receipts_keep_raw_prose(tmp_path, requires_revision):
    repository = EvidenceRepository(ROOT / "VIVI_MVP_Sample_Dataset")
    pack = repository.build("lead")
    original_pack = deepcopy(pack)
    diagnosis = json.loads((ROOT / "output/lead.v7.json").read_text())["diagnosis"]
    generated = {"design": _plan(pack, "design")}
    values = [diagnosis, generated["design"], _review(not requires_revision)]
    if requires_revision:
        generated["revise"] = _plan(pack, "revise")
        values.extend([generated["revise"], _review()])
    provider = ScriptedProvider(values)
    result = AgentRunner(repository, provider, tmp_path).run("lead")

    assert result["status"] == "completed"
    expected_stages = ["diagnose", "design", "review"]
    if requires_revision:
        expected_stages.extend(["revise", "re_review"])
    assert provider.stages == expected_stages
    assert not result["validation_errors"] and not result["review_errors"]

    contract = result["execution_contracts"][0]
    rendered = render_measurement(contract)
    assert contract["cadence"]["pairs_per_round"] == 7
    assert contract["cadence"]["total_scheduled_observations"] == 14
    measurement = result["plan"]["actions"][0]["measurement"]
    assert {field: measurement[field] for field in MEASUREMENT_FIELDS} == rendered
    assert "RAW_INCOMPLETE" not in json.dumps(result["plan"])
    for field in MEASUREMENT_FIELDS:
        assert rendered[field] in result["plan"]["monitoring_plan"]

    for stage in ["review"] + (["re_review"] if requires_revision else []):
        payload = provider.inputs[stage]
        review_contract = payload["execution_contracts"][0]
        review_measurement = payload["candidate"]["actions"][0]["measurement"]
        assert {field: review_measurement[field] for field in MEASUREMENT_FIELDS} == render_measurement(review_contract)
        assert "RAW_INCOMPLETE" not in json.dumps(payload["candidate"])

    with sqlite3.connect(tmp_path / "usage.sqlite") as connection:
        rows = connection.execute(
            "SELECT stage, result_json, provider_receipt_json FROM model_calls "
            "WHERE run_key=? AND stage IN ('design', 'revise') AND status='completed'",
            (result["run_id"],),
        ).fetchall()
    assert {row[0] for row in rows} == set(generated)
    for stage, result_json, provider_receipt_json in rows:
        paid_response = json.loads(result_json)
        assert paid_response == generated[stage]
        assert json.loads(provider_receipt_json)["actual_provider_calls"] == 0
        for field in MEASUREMENT_FIELDS:
            assert paid_response["actions"][0]["measurement"][field] == f"RAW_INCOMPLETE_{stage}_{field}"

    # 후속 관측이 없으면 개선 여부는 미확정이다.
    assert pack == original_pack
    assert result["metrics"] == original_pack["metrics"]
    assert {(metric["numerator"], metric["denominator"]) for metric in contract["primary_metrics"]} == {(0, 3)}
    unmeasured = evaluate_follow_up(contract, [])
    assert unmeasured["positive_signal"] is False
    assert unmeasured["causal_effect_established"] is False
    assert all(metric["status"] == "insufficient_data" and metric["numerator_change"] is None
               for metric in unmeasured["primary_metrics"])
