#!/usr/bin/env python3
"""과제의 고정 단가로 1회 호출 구조의 비용을 추산한다. 공급사 실청구액과 구분한다."""

import argparse
import json
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRICING_PATH = ROOT / "VIVI_MVP_Sample_Dataset" / "pricing_assumptions.json"
QUESTIONS = 75
CHANNELS = 3
OPERATIONAL_MONTHLY_M_CALL_CAP = 500
SCENARIOS = (
    "baseline_m_plus_api",
    "local_m_without_cache",
    "local_m_exact_cache_50pct",
    "budget_theoretical_max_m",
    "budget_operational_cap_m",
)


def decimal(value, name="value", *, positive=False):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return number


def count(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be a {'positive' if positive else 'nonnegative'} integer")
    return value


def as_json_number(value):
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value.quantize(Decimal("0.000001")))
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def assumptions(pricing, collection_rounds=None):
    prices = {
        item["cost_id"]: decimal(item["unit_cost_usd"], item["cost_id"], positive=item["cost_id"] == "AI-M")
        for item in pricing["cost_items"]
    }
    subscription = decimal(pricing["customer_monthly_subscription_usd"], "subscription", positive=True)
    target_margin = decimal(pricing["target_gross_margin_pct"], "target margin")
    if target_margin > 100:
        raise ValueError("target margin must not exceed 100 percent")
    maximum_cogs = min(
        decimal(pricing["maximum_cogs_per_customer_usd"], "maximum COGS"),
        subscription * (1 - target_margin / 100),
    )
    rounds = decimal(
        pricing["collection"]["weeks_per_month"] if collection_rounds is None else collection_rounds,
        "collection rounds", positive=True,
    )
    events = QUESTIONS * CHANNELS * rounds
    collection_cost = events * prices["COL001"]
    fixed_cost = prices["INF001"] + collection_cost
    available = max(Decimal(0), maximum_cogs - fixed_cost)
    theoretical_cap = int((available / prices["AI-M"]).to_integral_value(rounding=ROUND_FLOOR))
    return {
        "prices": prices, "subscription": subscription, "target_margin_pct": target_margin,
        "maximum_cogs": maximum_cogs, "rounds": rounds, "events": events,
        "collection_cost": collection_cost, "fixed_cost": fixed_cost,
        "available_call_budget": available, "theoretical_cap": theoretical_cap,
        "effective_cap": min(OPERATIONAL_MONTHLY_M_CALL_CAP, theoretical_cap),
    }


def project(pricing, name, customers, monthly_requests, *, collection_rounds=None):
    count(customers, "customers", positive=True)
    count(monthly_requests, "monthly requests")
    if name not in SCENARIOS:
        raise ValueError(f"Unknown scenario: {name}")
    basis = assumptions(pricing, collection_rounds)
    prices = basis["prices"]
    cached = monthly_requests // 2 if name == "local_m_exact_cache_50pct" else 0
    requested_calls = monthly_requests - cached
    budget_applied = name in {"budget_theoretical_max_m", "budget_operational_cap_m"}
    enforced_cap = basis["effective_cap"] if name == "budget_operational_cap_m" else basis["theoretical_cap"]
    m_calls = min(requested_calls, enforced_cap) if budget_applied else requested_calls
    fallback = requested_calls - m_calls
    api_calls = monthly_requests if name == "baseline_m_plus_api" else 0
    costs = {
        "infrastructure": prices["INF001"], "collections": basis["collection_cost"],
        "model_attempts": m_calls * prices["AI-M"],
        "external_api_calls": api_calls * prices["API001"],
    }
    cogs = sum(costs.values(), Decimal(0))
    within_budget = cogs <= basis["maximum_cogs"]
    within_operational_cap = m_calls <= basis["effective_cap"]
    return {
        "scenario": name,
        "customers": customers,
        "per_customer": {
            "logical_analysis_requests": monthly_requests,
            "projected_collection_rounds": basis["rounds"],
            "projected_collection_events": basis["events"],
            "maximum_m_attempts_per_request": 1,
            "projected_paid_m_calls": m_calls,
            "projected_paid_external_api_calls": api_calls,
            "assumed_exact_cache_hit_pct": 50 if name == "local_m_exact_cache_50pct" else 0,
            "projected_exact_cache_requests": cached,
            "projected_exact_cache_hit_pct": Decimal(cached) / monthly_requests * 100 if monthly_requests else None,
            "projected_budget_fallback_requests": fallback,
            "projected_budget_fallback_pct": Decimal(fallback) / monthly_requests * 100 if monthly_requests else None,
            "observed_cache_hit_pct": None,
            "observed_paid_model_calls": None,
            "observed_fallback_quality": None,
            "costs_usd": costs,
            "total_cogs_usd": cogs,
            "revenue_usd": basis["subscription"],
            "gross_margin_pct": (basis["subscription"] - cogs) / basis["subscription"] * 100,
            "meets_margin_target": within_budget,
            "within_operational_policy": within_budget and within_operational_cap,
            "budget_projection": {
                "hard_budget_applied_in_scenario": budget_applied,
                "operational_500_cap_applied_in_scenario": name == "budget_operational_cap_m",
                "maximum_cogs_usd": basis["maximum_cogs"],
                "reserved_infrastructure_and_collection_usd": basis["fixed_cost"],
                "available_for_all_paid_calls_usd": basis["available_call_budget"],
                "maximum_m_calls_if_no_other_paid_calls": basis["theoretical_cap"],
                "operational_monthly_m_call_cap": OPERATIONAL_MONTHLY_M_CALL_CAP,
                "effective_m_call_cap_if_no_other_paid_calls": basis["effective_cap"],
                "projected_m_calls_within_effective_cap": within_operational_cap,
                "remaining_after_projected_usage_usd": basis["maximum_cogs"] - cogs,
                "amount_over_budget_usd": max(Decimal(0), cogs - basis["maximum_cogs"]),
            },
        },
        "fleet": {
            "logical_analysis_requests": customers * monthly_requests,
            "projected_paid_m_calls": customers * m_calls,
            "projected_paid_external_api_calls": customers * api_calls,
            "projected_exact_cache_requests": customers * cached,
            "projected_budget_fallback_requests": customers * fallback,
            "total_cogs_usd": customers * cogs,
            "revenue_usd": customers * basis["subscription"],
            "gross_profit_usd": customers * (basis["subscription"] - cogs),
        },
    }


def build_report(pricing):
    basis = assumptions(pricing)
    weekly = next(item for item in pricing["collection"]["reference_frequencies"] if item["frequency"] == "weekly")
    return {
        "source": str(PRICING_PATH.relative_to(ROOT)),
        "architecture_source": "VIVI_아키텍처_정리_20261015.md",
        "currency": pricing["currency"],
        "scenario_type": pricing["scenario"],
        "measurement_status": "Projection only: this calculator makes no model calls or request replays. Historical runtime/evaluation usage is measured separately and may describe an earlier architecture.",
        "production_contract": {
            "maximum_paid_m_attempts_per_request": 1,
            "automatic_model_repair_calls": 0,
            "operational_monthly_m_call_cap": OPERATIONAL_MONTHLY_M_CALL_CAP,
            "cache_key": ["tenant_id", "brand_id", "request_scope", "dataset_version",
                          "business_context_hash", "prompt_version", "policy_version", "model_version", "output_schema_version"],
        },
        "collection_cost_basis": {
            "questions": QUESTIONS, "channels": CHANNELS,
            "events_per_round": QUESTIONS * CHANNELS,
            "weeks_per_month_from_source": basis["rounds"],
            "unit_collection_cost_usd": basis["prices"]["COL001"],
            "formula": "75 questions * 3 channels * 4.3 weeks/month * $0.004 = $3.870/month",
            "unrounded_expected_events_per_month": basis["events"],
            "calculated_collection_cost_usd": basis["collection_cost"],
            "source_displayed_events_per_month": weekly["collections_per_month"],
            "source_reference_cost_usd": weekly["cost_usd"],
            "cost_using_displayed_rounded_event_count_usd": decimal(weekly["collections_per_month"]) * basis["prices"]["COL001"],
            "rounding_note": "The reference displays 968 events, rounding the expected 967.5 events. Multiplying 968 by $0.004 gives $3.872; this report uses 225 * 4.3 without intermediate rounding, which exactly yields the source's $3.87. Actual months use integer event counts.",
            "infrastructure_allocation_usd": basis["prices"]["INF001"],
            "combined_fixed_cost_usd": basis["fixed_cost"],
            "average_detection_delay_days_assumption": weekly["avg_detection_delay_days"],
        },
        "assumptions": [
            "A generation-eligible request uses at most one M attempt combining evidence-backed judgment and action proposal. Schema/reference validation, normalization, and fallback are deterministic; no paid critic or repair loop is included.",
            "External API cost is zero only where supplied data and local structured queries actually replace the paid analysis. Mock execution is not a cost saving.",
            "The 50% exact-cache scenario is hypothetical, not measured. Reuse needs an exact key match and current action-state/duplicate checks. Similar historical experience does not count as an exact cache hit.",
            "Odd request counts round the hypothetical cached request count down to an integer; the reported projected cache rate reflects that rounding. Rates are null when there are zero requests.",
            "The theoretical-cap scenario demonstrates the mathematical ceiling, not permission to bypass the operational cap of 500. The operational scenario applies the smaller of the money-derived cap and 500.",
            "Budget fallback is distinct from an exact cache hit and from a successful model proposal. Its quality and business usefulness have not been measured here.",
            "Projected routes partition requests into model attempts, exact-cache reuse, and pre-call budget fallback. Additional no_action/insufficient_evidence savings are not assumed.",
            "A failed or invalid-output model attempt still costs one M call and ends in fallback without another model attempt. Post-call failures may increase fallback outcomes beyond the pre-call budget fallback count shown here.",
            "The baseline, local-without-cache, and 50%-cache rows show unthrottled demand costs. Rows that exceed the money/call budget are comparisons, not permitted runtime execution.",
            "All production, evaluation, background, and uncertain paid attempts must share the applicable budget; their extra usage reduces remaining admission capacity. No such additional calls are included in these base scenarios.",
            "Request growth requires recalculation; 15 topics do not imply a measured cache-hit rate or guarantee that generation counts stay fixed. Customer growth retains the source's $6 per-customer infrastructure allocation without volume discounts.",
            "Zero analysis requests still incur the modeled subscription, weekly collection, and infrastructure costs. Customer counts must be positive; request counts must be nonnegative integers.",
            "Model tier quality numbers are fictional task-level references. Combining judgment and proposal in one call does not establish measured quality, and fallback responses cannot inherit the model's quality figures.",
        ],
        "quality_assumptions": {
            task["task_id"]: {"metric": task["metric"], "assumed_m_value": task["M"], "measured_value": None}
            for task in pricing["model_tier_quality"]["tasks"] if task["task_id"] in {"diagnose", "draft_action"}
        },
        "hard_budget_contract": [
            "Reserve the actual month's planned collection and infrastructure costs before model admission. The 4.3-week average is a projection, not a guarantee for a five-collection month.",
            "Atomically admit at most one paid M attempt per request, with SDK retries disabled. Persist attempts and results so replay cannot silently create another paid attempt.",
            "Use min(500, floor(remaining monetary budget / M unit cost)); account for every other paid operation before admitting more calls.",
            "If no budget remains, return deterministic fallback or an explicit generation failure. Do not describe it as a new model-generated analysis or a cache hit.",
            "This calculator does not implement runtime enforcement, prove throughput, or calculate vendor token-based billing.",
        ],
        "scenarios": [project(pricing, name, customers, requests)
                      for name in SCENARIOS for customers in (100, 1000) for requests in (1000, 10000)],
        "five_collection_month_sensitivity": [
            project(pricing, name, 100, 1000, collection_rounds=5)
            for name in ("budget_theoretical_max_m", "budget_operational_cap_m")
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write JSON; relative paths resolve from the repository root.")
    args = parser.parse_args()
    pricing = json.loads(PRICING_PATH.read_text(encoding="utf-8"))
    report = json.dumps(build_report(pricing), ensure_ascii=False, indent=2, default=as_json_number) + "\n"
    if args.output:
        output = args.output if args.output.is_absolute() else ROOT / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
