"""참조와 구조를 규칙으로 검사한다. 내용의 타당성은 별도로 검토한다."""
from pydantic import ValidationError
from .models import ActionPlan, Diagnosis, Review
from .integration import validate_integration


def _requires_recollection(pack) -> bool:
    """보수적인 제품 정책이며 이 임계값은 통계적 신뢰도를 뜻하지 않는다."""
    return pack["quality"]["coverage"] < 0.8


def allowed_decisions(pack) -> list[str]:
    """수집률을 먼저 충족시키고 불필요한 개선 작업을 피하는 초기 정책.

    수집률 임계값은 통계적 신뢰도 기준이 아니다. 모든 적격 응답에서 추천되면 유지 관찰한다.
    """
    if _requires_recollection(pack):
        return ["needs_data"]
    recommendations = pack["metrics"].get("topic.target_recommendations", {})
    denominator = recommendations.get("denominator", 0)
    if denominator > 0 and recommendations.get("numerator") == denominator:
        return ["observe"]
    return ["recommend", "observe", "needs_data"]


def _decision_policy_error(decision, pack, path):
    allowed = allowed_decisions(pack)
    if decision in allowed:
        return []
    if allowed == ["needs_data"]:
        return [f"{path}.decision: coverage below 0.8 requires needs_data under the product data-quality policy"]
    return [f"{path}.decision: complete target recommendation coverage requires observe under the initial product decision policy"]


def validate_references(value, pack) -> list[str]:
    errors = []
    allowed = set(pack["allowed_evidence_ids"])
    metrics = pack["metrics"]
    questions = {q["question_id"] for q in pack["questions"]}
    channels = set(pack["quality"]["by_channel"])
    sources = {s["source_id"] for s in pack["sources"]}
    observations = {o["observation_id"] for o in pack["observations"] + pack["excluded"]}
    citations = {c["citation_id"] for o in pack["observations"] for c in o["citations"]}

    def walk(node, path="output"):
        if isinstance(node, dict):
            for key, val in node.items():
                here = f"{path}.{key}"
                if key in {"evidence_ids", "supporting_evidence_ids", "evidence_requests",
                           "observation_ids", "source_ids", "citation_ids"}:
                    for ref in val:
                        if ref not in allowed: errors.append(f"{here}: unknown/out-of-scope reference {ref}")
                        typed = {"source_ids": sources, "observation_ids": observations, "citation_ids": citations}.get(key)
                        if typed is not None and ref not in typed:
                            errors.append(f"{here}: wrong evidence type {ref}")
                elif key in {"metric_ids", "baseline_metric_ids", "secondary_metric_ids"}:
                    for ref in val:
                        if ref not in metrics: errors.append(f"{here}: unknown metric {ref}")
                elif key == "question_ids":
                    for ref in val:
                        if ref not in questions: errors.append(f"{here}: wrong question {ref}")
                elif key == "channels":
                    for ref in val:
                        if ref not in channels: errors.append(f"{here}: wrong channel {ref}")
                walk(val, here)
        elif isinstance(node, list):
            for i, child in enumerate(node): walk(child, f"{path}[{i}]")
    walk(value)
    return errors


def validate_diagnosis(value, pack):
    errors = validate_references(value, pack)
    try:
        diagnosis = Diagnosis.model_validate(value)
        errors.extend(_decision_policy_error(diagnosis.decision, pack, "diagnosis"))
        if not diagnosis.facts: errors.append("diagnosis.facts: evidence-backed facts required")
        for i, fact in enumerate(diagnosis.facts):
            if not fact.evidence_ids and not fact.metric_ids:
                errors.append(f"diagnosis.facts[{i}]: missing evidence")
    except ValidationError as exc:
        errors.append(f"diagnosis schema: {exc}")
    return errors


def validate_plan(value, pack) -> list[str]:
    errors = validate_references(value, pack)
    try:
        plan = ActionPlan.model_validate(value)
    except ValidationError as exc:
        return errors + [f"plan schema: {exc}"]
    errors.extend(_decision_policy_error(plan.decision, pack, "plan"))
    if len(plan.actions) > 1: errors.append("actions: at most one prioritized action allowed")
    if plan.decision == "recommend" and not plan.actions:
        errors.append("recommend requires at least one action")
    if plan.decision == "observe" and (plan.actions or not plan.monitoring_plan.strip()):
        errors.append("observe requires no actions and a monitoring plan")
    if plan.decision == "needs_data" and not plan.data_tasks:
        errors.append("needs_data requires explicit recollection tasks")
    sources = {s["source_id"]: s for s in pack["sources"]}
    owned_ids = {sid for sid, source in sources.items()
                 if source.get("brand_id") == pack["scope"]["target_brand_id"] and source.get("is_brand_owned")}
    for i, action in enumerate(plan.actions):
        path = f"actions[{i}]"
        errors.extend(f"{path}.{error}" for error in validate_integration(action.model_dump(), pack))
        assessed_ids = [a.source_id for a in action.target_asset.assessments]
        if len(assessed_ids) != len(set(assessed_ids)) or set(assessed_ids) != owned_ids:
            errors.append(f"{path}.target_asset.assessments: assess each target-owned source exactly once")
        candidates = {a.source_id for a in action.target_asset.assessments if a.reuse_decision == "inspect_for_reuse"}
        if candidates != set(action.target_asset.source_ids):
            errors.append(f"{path}.target_asset: source_ids must match all inspect_for_reuse candidates")
        if any(not a.rationale.strip() for a in action.target_asset.assessments):
            errors.append(f"{path}.target_asset.assessments: each asset needs a substantive reuse/exclusion reason")
        if not action.evidence_ids: errors.append(f"{path}: missing evidence")
        if not action.question_ids or not action.channels:
            errors.append(f"{path}: question/channel scope required")
        if len(action.steps) < 2 or len(action.acceptance_criteria) < 2:
            errors.append(f"{path}: concrete steps and acceptance criteria required")
        if not action.deliverables or not any(len(d.example_content.strip()) >= 40 for d in action.deliverables):
            errors.append(f"{path}: usable draft/example content required")
        if not action.measurement.baseline_metric_ids:
            errors.append(f"{path}: measurement baseline required")
        if action.prerequisites and action.readiness == "ready_to_start":
            errors.append(f"{path}: unresolved prerequisites require needs_input or blocked readiness")
        for sid in action.target_asset.source_ids:
            source = sources.get(sid)
            if source and (source.get("brand_id") != pack["scope"]["target_brand_id"] or not source.get("is_brand_owned")):
                errors.append(f"{path}: cannot edit a competitor/unattributed source {sid}")
    expected_missing = {(o["question_id"], o["channel"]) for o in pack["excluded"] if o["reason"] != "superseded"}
    for task in plan.data_tasks:
        if (task.question_id, task.channel) not in expected_missing:
            errors.append(f"data_tasks: {task.question_id}/{task.channel} is not an excluded pair")
    if _requires_recollection(pack):
        planned_pairs = {(task.question_id, task.channel) for task in plan.data_tasks}
        for question_id, channel in sorted(expected_missing - planned_pairs):
            errors.append(f"data_tasks: coverage below 0.8 requires recollection of {question_id}/{channel}")
    return errors


def validate_review(value, pack) -> list[str]:
    """검토 모델이 확인됐다고 선언하는 것만으로 검증 근거가 생기지는 않는다."""
    review = Review.model_validate(value)
    context = pack["business_context"]
    receipts = {fact["receipt_id"] for fact in context.get("verified_product_facts", [])
                if isinstance(fact, dict) and fact.get("receipt_id") and fact.get("statement")}
    if context.get("evidence_receipt") and isinstance(context.get("verified_web_form_auto_registration"), bool):
        receipts.add(context["evidence_receipt"])
    errors = []
    for i, claim in enumerate(review.claim_checks):
        if claim.status == "verified" and claim.asserted_as_fact and claim.verification_ref not in receipts:
            errors.append(f"claim_checks[{i}]: verified factual claim has no valid business-context receipt")
    return errors
