"""모델이 선택한 지표를 동일 질문·채널 페어의 재현 가능한 수집 계약으로 만든다.

자유 서술 측정 문장을 해석해 고치지 않으며, 명시된 주·보조 지표의 원래 분모를 유지한다.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any


VERSION = "paired-measurement.v1"


def _pair(row: dict) -> tuple[str, str]:
    return row["question_id"], row["channel"]


def _pairs(values) -> list[dict]:
    return [{"question_id": q, "channel": c} for q, c in sorted(values)]


def _selected_strings(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x for x in value):
        raise ValueError(f"{name} must contain explicit, nonempty identifiers")
    if len(value) != len(set(value)):
        raise ValueError(f"{name} contains duplicate identifiers")
    return value


def _selector(metric_id: str, target: str) -> dict:
    parts = metric_id.split(".")
    if len(parts) == 3 and parts[0] == "source" and parts[2] == "citation_responses":
        return {"kind": "source_citation", "source_ids": [parts[1]]}
    if len(parts) == 3 and parts[0] == "brand":
        kind, brand = parts[2], parts[1]
    else:
        kind, brand = parts[-1].removeprefix("target_"), target
    if kind not in {"mentions", "recommendations", "owned_citations", "cited_without_mention"}:
        raise ValueError(f"Unsupported measurement reducer: {metric_id}")
    return {"kind": kind, "brand_id": brand}


def _matches(row: dict, selector: dict) -> bool:
    if selector["kind"] == "source_citation":
        return any(c["source_id"] in selector["source_ids"] for c in row.get("citations", []))
    brand = selector["brand_id"]
    mentioned = any(m["brand_id"] == brand for m in row.get("mentions", []))
    if selector["kind"] == "mentions":
        return mentioned
    if selector["kind"] == "recommendations":
        return any(m["brand_id"] == brand and m["mention_context"] == "recommended" for m in row.get("mentions", []))
    owned = any(c.get("source", {}).get("is_brand_owned") is True and c["source"].get("brand_id") == brand
                for c in row.get("citations", []))
    return owned and (not mentioned if selector["kind"] == "cited_without_mention" else True)


def compile_measurement(action: dict, pack: dict) -> dict:
    """독립된 측정 계약을 반환하고 기준선이 부적합하면 ValueError를 발생시킨다.

    액션의 질문과 채널 조합이 실행 범위다. 주 지표는 그 범위 안에 있어야 하며, 보조 지표도
    원래 분모 전체를 수집해야 한다. 주 지표는 하나 이상 필요하다. 과거 자료나 분모가 0인
    기준선은 호출자가 명시적으로 바꿔야 하며 여기서 범위를 줄이지 않는다.
    """
    question_ids = _selected_strings(action.get("question_ids"), "action.question_ids")
    channels = _selected_strings(action.get("channels"), "action.channels")
    measurement = action.get("measurement", {})
    primary_ids = _selected_strings(measurement.get("baseline_metric_ids"), "baseline_metric_ids")
    secondary_ids = measurement.get("secondary_metric_ids")
    if not isinstance(secondary_ids, list):
        raise ValueError("secondary_metric_ids must be an explicit list, including [] when unused")
    if secondary_ids:
        _selected_strings(secondary_ids, "secondary_metric_ids")
    if set(primary_ids) & set(secondary_ids):
        raise ValueError("A metric cannot be both primary and secondary")
    metric_ids = [*primary_ids, *secondary_ids]
    questions = {q["question_id"]: q for q in pack["questions"]}
    known_channels = set(pack["quality"]["by_channel"])
    if set(question_ids) - questions.keys() or set(channels) - known_channels:
        raise ValueError("Action contains unknown question/channel scope")
    action_pairs = {(q, c) for q in question_ids for c in channels}
    current = {row["observation_id"]: row for row in pack["observations"]}
    current_pairs = {_pair(row): row for row in current.values()}
    target = pack["scope"]["target_brand_id"]
    primary, secondary = [], []
    sampling = set()
    for metric_id in metric_ids:
        if metric_id not in pack["metrics"]:
            raise ValueError(f"Unknown baseline metric: {metric_id}")
        metric = pack["metrics"][metric_id]
        ids = metric.get("observation_ids", [])
        if not ids or len(ids) != len(set(ids)) or len(ids) != metric["denominator"]:
            raise ValueError(f"Baseline has a missing or inconsistent denominator: {metric_id}")
        if set(ids) - current.keys():
            raise ValueError(f"Baseline must resolve to current eligible observations: {metric_id}")
        rows = [current[oid] for oid in ids]
        metric_pairs = {_pair(row) for row in rows}
        if len(metric_pairs) != len(rows):
            raise ValueError(f"Baseline contains duplicate question/channel pairs: {metric_id}")
        for row in rows:
            if row["collection_status"] != "complete" or row["response_language"] != questions[row["question_id"]]["language"]:
                raise ValueError(f"Baseline contains ineligible observations: {metric_id}")
        selector = _selector(metric_id, target)
        supporting = [row["observation_id"] for row in rows if _matches(row, selector)]
        if len(supporting) != metric["numerator"] or set(supporting) != set(metric["supporting_observation_ids"]):
            raise ValueError(f"Baseline numerator disagrees with its evidence: {metric_id}")
        role = "primary" if metric_id in primary_ids else "secondary"
        if role == "primary":
            parts = metric_id.split(".")
            if (len(parts) != 3 or parts[0] != "question" or parts[1] not in question_ids
                    or not parts[2].startswith("target_") or not metric_pairs <= action_pairs):
                raise ValueError(f"Primary metric must be a selected question target metric within the action channel scope: {metric_id}")
        compiled = {
            "metric_id": metric_id, "numerator": metric["numerator"], "denominator": metric["denominator"],
            "question_channel_pairs": _pairs(metric_pairs), "baseline_observation_ids": list(ids),
            "baseline_supporting_observation_ids": supporting, "role": role, "selector": selector,
            "scope_relation": "within_action" if metric_pairs <= action_pairs else "extends_beyond_action",
        }
        (primary if role == "primary" else secondary).append(compiled)
        sampling.update(metric_pairs)
    if not primary:
        raise ValueError("At least one baseline metric must fit the selected action question/channel scope")

    asset = action.get("target_asset", {})
    source_ids = asset.get("source_ids", [])
    if not isinstance(source_ids, list) or any(not isinstance(s, str) for s in source_ids) or len(source_ids) != len(set(source_ids)):
        raise ValueError("target_asset.source_ids must be distinct source identifiers")
    sources = {source["source_id"]: source for source in pack["sources"]}
    for source_id in source_ids:
        source = sources.get(source_id)
        if not source or not source["is_brand_owned"] or source["brand_id"] != target:
            raise ValueError(f"Selected asset must be an explicitly target-owned source: {source_id}")
    asset_rows = [current_pairs[pair] for pair in sorted(action_pairs & current_pairs.keys())]
    source_selector = {"kind": "source_citation", "source_ids": list(source_ids)}
    source_check = {
        "source_ids": list(source_ids),
        "requires_final_source_selection": asset.get("mode") == "verify_then_choose",
        "new_asset_requires_source_id_registration": asset.get("mode") in {"create", "verify_then_choose"} or not source_ids,
        "registration_rule": "Before publication, register the exact published URL and its target-owned source_id; if revising, confirm the selected existing source_id. Candidate source IDs are not a final selection.",
        "status": "ready" if source_ids and asset.get("mode") != "verify_then_choose" else "pending_asset_selection_or_registration",
        "question_channel_pairs": _pairs({_pair(row) for row in asset_rows}),
        "baseline_observation_ids": [row["observation_id"] for row in asset_rows],
        "baseline_citing_observation_ids": [row["observation_id"] for row in asset_rows if _matches(row, source_selector)],
        "denominator": len(asset_rows),
        "interpretation": "Track citations to the selected published source IDs separately from any target-owned citation; one response contributes at most one citation hit.",
    }
    sampling.update(_pair(row) for row in asset_rows)
    sampling_plan = []
    for question_id, channel in sorted(sampling):
        question = questions[question_id]
        sampling_plan.append({
            "question_id": question_id, "channel": channel, "query": question["query"],
            "response_language": question["language"], "country": question.get("country"),
            "baseline_observation_id": current_pairs[(question_id, channel)]["observation_id"],
        })
    return deepcopy({
        "version": VERSION, "scope": pack["scope"],
        "action_scope": {"question_ids": question_ids, "channels": channels, "question_channel_pairs": _pairs(action_pairs)},
        "primary_metrics": primary, "secondary_metrics": secondary, "sampling_plan": sampling_plan,
        "source_citation_check": source_check,
        "cadence": {"anchor": "confirmed_publication_at", "days_after_publication": [7, 14],
                    "collections_per_pair_per_scheduled_day": 1, "pairs_per_round": len(sampling_plan),
                    "total_scheduled_observations": len(sampling_plan) * 2},
        "normalization_rules": {
            "group_by": ["question_id", "channel", "follow_up_round"],
            "canonical_preference": ["complete_over_partial_over_failed", "matching_question_language", "latest_collected_at", "greatest_observation_id"],
            "eligible": "complete with response_language equal to the baseline question language",
            "exclude": ["superseded", "partial", "failed", "language_mismatch"],
            "missing_is_absence": False, "count_unit": "one eligible response, not mention or citation rows",
            "paired_denominator": "Compare the exact baseline question/channel pairs. Missing pairs produce insufficient_data; never keep a larger denominator or replace a missing response with zero.",
            "hold_constant": ["question query", "question language", "country", "AI channel", "available model and collection settings; record any change"],
        },
        "interpretation_rules": {
            "primary_positive_signal": "At least one primary metric increases on a complete paired sample; secondary-only change does not satisfy action success.",
            "secondary_positive_signal": "Context only; collect every listed pair and report separately from primary action outcomes.",
            "unchanged": "Equal numerator on an unchanged paired denominator is no observed improvement; unchanged zero exposure is not a positive signal.",
            "after_day_14": "If primary metrics show no observed improvement, review the selected asset and hypothesis. Missing data require recollection, not an effectiveness conclusion.",
            "attribution": "Descriptive before/after signal only; two small rounds do not establish causality or statistical significance.",
        },
    })


def render_measurement(contract: dict) -> dict[str, str]:
    """확정된 측정 계약에서 세 가지 표시 문장을 만든다.

    추가 모델 판단은 하지 않는다. 호출자는 모델의 측정 문장 원문을 감사용 기록에 남길 수 있다.
    """
    primary = contract["primary_metrics"]
    secondary = contract["secondary_metrics"]
    cadence = contract["cadence"]
    source = contract["source_citation_check"]

    def pair_text(pairs):
        return ", ".join(f"{pair['question_id']}/{pair['channel']}" for pair in pairs)

    def metric_text(metric):
        return (f"{metric['metric_id']} 기준선 {metric['numerator']}/{metric['denominator']}, "
                f"고정 분모 {metric['denominator']}개 응답({pair_text(metric['question_channel_pairs'])})")

    primary_text = "; ".join(metric_text(metric) for metric in primary)
    secondary_text = "; ".join(metric_text(metric) for metric in secondary)
    days = "·".join(f"{day}일 후" for day in cadence["days_after_publication"])
    sources = ", ".join(source["source_ids"]) or "신규 자산의 source ID 미등록"
    if source["status"] != "ready":
        source_gate = (
            f"자산 선택·등록 상태는 {source['status']}입니다(후보: {sources}). "
            "게시 전에 최종 자산의 정확한 URL과 자사 소유 source ID 등록을 완료하고, "
            "기존 자산 개정이면 실제 선택한 source ID를 확정합니다. "
            "후보 목록이나 등록 요청만으로 완료 처리하지 않으며, 이 조건이 해결될 때까지 후속 수집을 보류합니다."
        )
    else:
        source_gate = (
            f"게시 전에 실제 발행 자산의 URL과 선택한 기존 source ID({sources})를 확인합니다. "
            "새 자산으로 변경하면 새 source ID 등록을 완료하기 전까지 후속 수집을 보류합니다."
        )
    secondary_scope = (
        f"보조 지표는 {secondary_text}입니다. 각 지표에 나열된 전체 질문/채널을 별도로 수집하고 참고용으로만 보고합니다."
        if secondary else "보조 지표는 선택하지 않았습니다."
    )
    follow_up = (
        f"확정 게시일을 기준으로 {days} 각각 1회, 질문/채널 페어마다 1개 응답을 수집합니다. "
        f"회차별 고정 수집 범위는 {cadence['pairs_per_round']}페어"
        f"({pair_text(contract['sampling_plan'])})이며, 총 {cadence['total_scheduled_observations']}개 응답을 예정합니다. "
        f"주요 지표는 {primary_text}입니다. {secondary_scope} "
        "질문 원문·언어·국가·AI 채널을 기준선과 동일하게 유지하고 모델·수집 설정 변경을 기록합니다. "
        "동일 회차·질문·채널의 중복 수집은 계약의 canonical_preference 순서로 1건만 선택합니다. "
        "complete이면서 질문 언어와 response_language가 일치하는 응답만 적격입니다. "
        "superseded, partial, failed, language_mismatch는 제외하고 누락을 미노출 0으로 대체하지 않습니다. "
        "고정 페어가 하나라도 빠진 지표는 insufficient_data로 보고하고 실제 관측 분모를 표시하며, 기준선 고정 분모와 비교해 개선을 판정하지 않습니다. "
        f"{source_gate} 선택한 최종 source ID의 인용 여부는 일반적인 자사 인용 지표와 구분하여 기록합니다."
    )
    thresholds = "; ".join(
        f"{metric['metric_id']} {metric['numerator']}/{metric['denominator']} → "
        f"{metric['numerator'] + 1}/{metric['denominator']} 이상"
        for metric in primary if metric["numerator"] < metric["denominator"]
    )
    positive_signal = (
        "모든 주요 지표의 기준선과 동일한 질문/채널 고정 페어가 적격 응답으로 완전히 확보된 회차에서만 판단합니다. "
        + (f"주요 지표 중 하나 이상이 다음 조건을 만족하면 긍정 신호입니다: {thresholds}. "
           if thresholds else "모든 주요 지표가 이미 100%이므로 현재 계약에는 증가 가능한 주요 지표가 없으며, 변화만으로 개선을 선언하지 않습니다. ")
        + "주요 지표의 분모가 줄거나 누락이 있으면 insufficient_data이며 성공으로 처리하지 않습니다. "
        "보조 지표의 개선만으로는 액션 성공으로 처리하지 않습니다. "
        "동일 분모에서 분자가 그대로이거나 줄면 개선이 아니며, 0 노출 유지도 긍정 신호가 아닙니다. "
        "이는 게시 전후의 기술적 비교 신호이며 인과 효과나 통계적 유의성을 입증하지 않습니다."
    )
    stop_or_revise = (
        "각 회차에서 고정 페어가 빠진 지표는 효과 결론을 내리지 말고 해당 질문/채널을 추가 수집합니다. "
        "partial·failed·언어 불일치를 0 노출로 간주하거나 더 작은 분모로 성공률을 비교하지 않습니다. "
        "보조 지표의 결측은 해당 보조 지표의 판정을 보류하며, 주요 지표의 결과를 대체하지 않습니다. "
        "게시 후 14일 시점의 주요 지표가 모두 완전한 고정 표본인데도 하나도 개선되지 않았거나 악화되면 "
        "선택한 자산·제목·서두·FAQ 및 개입 가설을 재검토합니다. "
        "표본이 불완전하면 실패나 무효과를 선언하지 않고 추가 수집이 우선입니다. "
        f"{source_gate}"
    )
    return {"follow_up": follow_up, "positive_signal": positive_signal, "stop_or_revise": stop_or_revise}


def evaluate_follow_up(contract: dict, observations: list[dict]) -> dict:
    """후속 수집 한 회차를 확정된 기준선 페어와 비교한다.

    입력은 EvidenceRepository와 같은 mentions/citations 구조이며 출처 소유 정보도 포함한다.
    이 함수는 데이터를 수집하거나 추천 액션을 실행하지 않는다.
    """
    samples = {_pair(row): row for row in contract["sampling_plan"]}
    snapshots = {row.get("source_snapshot_id") for row in observations if _pair(row) in samples and row.get("source_snapshot_id")}
    if len(snapshots) > 1:
        raise ValueError("Evaluate one follow-up snapshot at a time")
    groups: dict[tuple, list[dict]] = {}
    for row in observations:
        if _pair(row) in samples:
            groups.setdefault(_pair(row), []).append(row)
    eligible, excluded = {}, []
    for pair, attempts in groups.items():
        language = samples[pair]["response_language"]
        def order(row):
            return ({"complete": 2, "partial": 1, "failed": 0}[row["collection_status"]],
                    row["response_language"] == language, datetime.fromisoformat(row["collected_at"]), row["observation_id"])
        canonical = max(attempts, key=order)
        for row in attempts:
            reason = ("superseded" if row is not canonical else row["collection_status"] if row["collection_status"] != "complete"
                      else "language_mismatch" if row["response_language"] != language else None)
            if reason:
                excluded.append({"observation_id": row["observation_id"], "reason": reason})
            else:
                eligible[pair] = row
    result = {"primary_metrics": [], "secondary_metrics": [], "excluded": excluded}
    for role in ("primary", "secondary"):
        for metric in contract[role + "_metrics"]:
            pairs = {_pair(row) for row in metric["question_channel_pairs"]}
            matched = pairs & eligible.keys()
            numerator = sum(_matches(eligible[pair], metric["selector"]) for pair in matched)
            delta = numerator - metric["numerator"] if matched == pairs else None
            status = "insufficient_data" if delta is None else "improved" if delta > 0 else "declined" if delta < 0 else "unchanged"
            result[role + "_metrics"].append({
                "metric_id": metric["metric_id"], "role": role, "status": status,
                "numerator": numerator, "denominator": len(matched), "expected_denominator": metric["denominator"],
                "baseline_numerator": metric["numerator"], "baseline_denominator": metric["denominator"],
                "missing_question_channel_pairs": _pairs(pairs - matched), "numerator_change": delta,
            })
    primary_statuses = [metric["status"] for metric in result["primary_metrics"]]
    result["positive_signal"] = "improved" in primary_statuses and "insufficient_data" not in primary_statuses
    result["causal_effect_established"] = False
    return result
