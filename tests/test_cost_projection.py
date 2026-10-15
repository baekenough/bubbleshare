"""과제에 제시된 독립 계산 예시로 예산 경계를 검증한다."""

from copy import deepcopy
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("project_costs", ROOT / "scripts/project_costs.py")
costs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(costs)


@pytest.fixture
def pricing():
    return json.loads((ROOT / "VIVI_MVP_Sample_Dataset/pricing_assumptions.json").read_text())


@pytest.mark.parametrize("name,cogs,margin,calls,cached,fallback", [
    ("baseline_m_plus_api", "39.87", "60.13", 1000, 0, 0),
    ("local_m_without_cache", "27.87", "72.13", 1000, 0, 0),
    ("local_m_exact_cache_50pct", "18.87", "81.13", 500, 500, 0),
    ("budget_theoretical_max_m", "19.986", "80.014", 562, 0, 438),
    ("budget_operational_cap_m", "18.87", "81.13", 500, 0, 500),
])
def test_architecture_worked_examples(pricing, name, cogs, margin, calls, cached, fallback):
    result = costs.project(pricing, name, 100, 1000)
    customer = result["per_customer"]
    assert customer["total_cogs_usd"] == Decimal(cogs)
    assert customer["gross_margin_pct"] == Decimal(margin)
    assert customer["projected_paid_m_calls"] == calls
    assert customer["projected_exact_cache_requests"] == cached
    assert customer["projected_budget_fallback_requests"] == fallback
    assert calls + cached + fallback == 1000
    assert result["fleet"]["total_cogs_usd"] == Decimal(cogs) * 100


def test_562_is_integer_ceiling_not_operational_permission(pricing):
    customer = costs.project(pricing, "budget_theoretical_max_m", 1, 1000)["per_customer"]
    assert customer["total_cogs_usd"] <= Decimal("20")
    assert customer["total_cogs_usd"] + Decimal("0.018") > Decimal("20")
    assert customer["budget_projection"]["remaining_after_projected_usage_usd"] == Decimal("0.014")
    assert not customer["within_operational_policy"]
    assert customer["meets_margin_target"]


def test_actual_collection_basis_is_unrounded_967_5_events(pricing):
    basis = costs.build_report(pricing)["collection_cost_basis"]
    assert basis["unrounded_expected_events_per_month"] == Decimal("967.5")
    assert basis["calculated_collection_cost_usd"] == Decimal("3.87")
    assert basis["source_displayed_events_per_month"] == 968
    assert basis["cost_using_displayed_rounded_event_count_usd"] == Decimal("3.872")
    assert basis["combined_fixed_cost_usd"] == Decimal("9.87")


def test_five_collection_month_reserves_more_than_average(pricing):
    theoretical = costs.project(pricing, "budget_theoretical_max_m", 1, 1000, collection_rounds=5)["per_customer"]
    operational = costs.project(pricing, "budget_operational_cap_m", 1, 1000, collection_rounds=5)["per_customer"]
    assert theoretical["costs_usd"]["collections"] == Decimal("4.5")
    assert theoretical["projected_paid_m_calls"] == 527
    assert theoretical["total_cogs_usd"] + Decimal("0.018") > Decimal("20")
    assert operational["projected_paid_m_calls"] == 500
    assert operational["total_cogs_usd"] == Decimal("19.50")
    assert operational["gross_margin_pct"] == Decimal("80.50")


def test_request_growth_recalculates_demand_and_budget_fallback(pricing):
    cache = costs.project(pricing, "local_m_exact_cache_50pct", 1000, 10000)
    capped = costs.project(pricing, "budget_operational_cap_m", 1000, 10000)
    assert cache["per_customer"]["projected_paid_m_calls"] == 5000
    assert cache["per_customer"]["total_cogs_usd"] == Decimal("99.87")
    assert not cache["per_customer"]["meets_margin_target"]
    assert cache["fleet"]["revenue_usd"] == 100000
    assert capped["per_customer"]["projected_budget_fallback_requests"] == 9500
    assert capped["per_customer"]["projected_exact_cache_requests"] == 0
    assert capped["fleet"]["total_cogs_usd"] == 18870


def test_zero_requests_keep_fixed_cost_without_undefined_rates(pricing):
    for name in costs.SCENARIOS:
        customer = costs.project(pricing, name, 1, 0)["per_customer"]
        assert customer["total_cogs_usd"] == Decimal("9.87")
        assert customer["projected_paid_m_calls"] == 0
        assert customer["projected_budget_fallback_requests"] == 0
        assert customer["projected_exact_cache_hit_pct"] is None
        assert customer["projected_budget_fallback_pct"] is None


def test_small_odd_request_count_does_not_invent_half_a_call(pricing):
    customer = costs.project(pricing, "local_m_exact_cache_50pct", 1, 3)["per_customer"]
    assert customer["projected_exact_cache_requests"] == 1
    assert customer["projected_paid_m_calls"] == 2
    assert customer["total_cogs_usd"] == Decimal("9.906")


@pytest.mark.parametrize("customers,requests", [(0, 1000), (-1, 1000), (1.5, 1000),
                                                (True, 1000), (1, -1), (1, 0.5), (1, False)])
def test_invalid_request_and_customer_counts_rejected(pricing, customers, requests):
    with pytest.raises(ValueError):
        costs.project(pricing, "budget_operational_cap_m", customers, requests)


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_collection_rounds_must_be_finite_and_positive(pricing, value):
    with pytest.raises(ValueError):
        costs.project(pricing, "budget_operational_cap_m", 1, 1000, collection_rounds=value)


def test_zero_model_price_and_zero_subscription_cannot_bypass_budget(pricing):
    changed = deepcopy(pricing)
    next(row for row in changed["cost_items"] if row["cost_id"] == "AI-M")["unit_cost_usd"] = 0
    with pytest.raises(ValueError):
        costs.project(changed, "budget_operational_cap_m", 1, 1000)
    changed = deepcopy(pricing)
    changed["customer_monthly_subscription_usd"] = 0
    with pytest.raises(ValueError):
        costs.project(changed, "budget_operational_cap_m", 1, 1000)


def test_projection_remains_explicitly_unmeasured_and_runs_outside_repo(pricing, tmp_path):
    output = tmp_path / "costs.json"
    subprocess.run([sys.executable, str(ROOT / "scripts/project_costs.py"), "--output", str(output)],
                   cwd=tmp_path, check=True)
    report = json.loads(output.read_text())
    expected = json.loads(json.dumps(costs.build_report(pricing), default=costs.as_json_number))
    assert report == expected
    assert len(report["scenarios"]) == 20
    for scenario in report["scenarios"]:
        assert scenario["per_customer"]["observed_cache_hit_pct"] is None
        assert scenario["per_customer"]["observed_paid_model_calls"] is None
        assert scenario["per_customer"]["observed_fallback_quality"] is None
