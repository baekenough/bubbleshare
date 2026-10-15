"""상태를 보존하는 LangGraph 분석·설계·검토 흐름."""
from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from . import prompts
from .evidence import EvidenceRepository
from .ledger import BudgetExceeded, Ledger
from .models import ActionPlan, Diagnosis, Review
from .integration import verified_integration
from .measurement import compile_measurement, render_measurement
from .validation import allowed_decisions, validate_diagnosis, validate_plan, validate_review


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def render_metric_facts(diagnosis: dict, pack: dict) -> dict:
    """설계와 표시 전에 정량 사실을 계산된 값에 연결한다.

    모델 원문은 감사용 영수증과 추적 기록에 남긴다. 정성적 관측 해석과 가설은 모델이 맡는다.
    """
    result = json.loads(json.dumps(diagnosis))
    for fact in result["facts"]:
        if fact["metric_ids"]:
            fact["statement"] = "; ".join(
                f"{metric_id}: {pack['metrics'][metric_id]['numerator']}/"
                f"{pack['metrics'][metric_id]['denominator']} "
                f"({pack['metrics'][metric_id]['description']})"
                for metric_id in fact["metric_ids"]
            )
    return result


def constrain_schema(schema: dict, evidence: dict) -> dict:
    """생성 전에 선택 가능한 값을 제한하고 생성 후 검증도 유지한다."""
    schema = json.loads(json.dumps(schema))
    reference_choices = {
        "evidence_ids": evidence["allowed_evidence_ids"],
        "supporting_evidence_ids": evidence["allowed_evidence_ids"],
        "evidence_requests": evidence["allowed_evidence_ids"],
        "metric_ids": list(evidence["metrics"]),
        "baseline_metric_ids": list(evidence["metrics"]),
        "secondary_metric_ids": list(evidence["metrics"]),
        "question_ids": [q["question_id"] for q in evidence["questions"]],
    }

    def constrain_references(node):
        if isinstance(node, dict):
            for name, field in node.get("properties", {}).items():
                if name in reference_choices:
                    values = reference_choices[name]
                    if values:
                        field["items"] = {"type": "string", "enum": values}
                    else:
                        field["maxItems"] = 0
            for child in node.values():
                constrain_references(child)
        elif isinstance(node, list):
            for child in node:
                constrain_references(child)
    constrain_references(schema)
    measurement = schema.get("$defs", {}).get("Measurement")
    if measurement:
        measurement["properties"]["baseline_metric_ids"]["items"] = {
            "type": "string", "enum": [mid for mid in evidence["metrics"] if mid.startswith("question.")]
        }
    procedure = schema.get("$defs", {}).get("IntegrationProcedure")
    known_route = verified_integration(evidence["business_context"])
    if procedure and known_route:
        for field, value in known_route.items():
            procedure["properties"][field]["enum"] = [value]
    properties = schema.get("properties", {})
    if "decision" in properties:
        decisions = allowed_decisions(evidence)
        properties["decision"]["enum"] = decisions
        if "actions" in properties and decisions in (["observe"], ["needs_data"]):
            properties["actions"]["maxItems"] = 0
    asset = schema.get("$defs", {}).get("ProductAsset") or schema.get("$defs", {}).get("Asset")
    if asset:
        owned = [source["source_id"] for source in evidence["sources"]
                 if source.get("is_brand_owned")
                 and source.get("brand_id") == evidence["scope"]["target_brand_id"]]
        field = asset["properties"]["source_ids"]
        if owned:
            field["items"] = {"type": "string", "enum": owned}
            schema["$defs"]["AssetAssessment"]["properties"]["source_id"]["enum"] = owned
        else:
            field["maxItems"] = 0
    return schema


def prompt_evidence(pack: dict) -> dict:
    """출처 메타데이터를 인용마다 반복하지 않고 근거를 전달한다."""
    observations = []
    for o in pack["observations"]:
        observations.append({
            k: o[k] for k in ("observation_id", "question_id", "channel", "response_text", "collected_at")
        } | {"mentions": o["mentions"], "citations": [
            {k: v for k, v in c.items() if k != "source"} for c in o["citations"]
        ]})
    comparison = {k: v for k, v in pack["previous_comparison"].items() if k != "previous_observations"}
    return {
        "scope": pack["scope"], "business_context": pack["business_context"],
        "allowed_evidence_ids": pack["allowed_evidence_ids"],
        "questions": pack["questions"], "quality": pack["quality"],
        "metrics": {key: {k: v for k, v in metric.items()
                             if k not in {"observation_ids", "supporting_observation_ids"}}
                    for key, metric in pack["metrics"].items()},
        "observations": observations, "sources": pack["sources"],
        "excluded": [{k: o.get(k) for k in ("observation_id", "question_id", "channel", "reason")}
                     for o in pack["excluded"]],
        "previous_comparison": comparison, "capabilities": pack["capabilities"],
        "execution_requirements": {"verified_integration": verified_integration(pack["business_context"])},
        "decision_policy": {"minimum_coverage": 0.8, "allowed_decisions": allowed_decisions(pack),
                            "note": "Initial product policy; not statistical confidence."},
    }


class AgentState(TypedDict, total=False):
    diagnosis: dict
    expanded_evidence: list
    plan: dict
    execution_contracts: list
    review: dict | None
    validation_errors: list[str]
    review_errors: list[str]
    execution_error: dict
    decision: str
    repair_slots: dict
    status: str
    events: list[str]


class AgentRunner:
    def __init__(self, repository: EvidenceRepository, provider, state_dir: Path):
        self.repository, self.provider = repository, provider
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(self.state_dir / "usage.sqlite")

    def key(self, pack):
        # 프롬프트뿐 아니라 구현 변경도 기존 판단 캐시를 무효화한다.
        code = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in Path(__file__).parent.glob("*.py") if p.name not in {"cli.py", "evaluation.py"}}
        return digest({"scope": pack["scope"], "dataset": pack["dataset_hash"],
                       "context": pack["context_hash"], "model": self.provider.model,
                       "provider": getattr(self.provider, "cache_identity", type(self.provider).__name__),
                       "prompt_version": prompts.VERSION, "code": code})

    @contextmanager
    def _run_lock(self, key):
        locks = self.state_dir / "locks"
        locks.mkdir(exist_ok=True)
        with (locks / f"{key}.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(stream, fcntl.LOCK_UN)

    def call(self, key, stage, output_type, payload, reservation_id=None):
        prompt = prompts.render(stage, payload)
        schema = constrain_schema(output_type.model_json_schema(), payload["evidence"])
        prompt_hash = digest({"prompt": prompt, "schema": schema, "model": self.provider.model,
                              "provider": getattr(self.provider, "cache_identity", type(self.provider).__name__)})
        existing = self.ledger.get_completed(key, stage, prompt_hash)
        if existing is not None:
            return output_type.model_validate(existing).model_dump()
        if reservation_id:
            self.ledger.bind_reservation(reservation_id, stage, prompt_hash)
            receipt = reservation_id
        else:
            receipt = self.ledger.reserve(key, stage, prompt_hash)
            existing = self.ledger.get_completed(key, stage, prompt_hash)
            if existing is not None:
                return output_type.model_validate(existing).model_dump()
        try:
            result = self.provider.generate(stage, prompt, schema)
        except Exception as exc:
            self.ledger.fail(receipt, type(exc).__name__)
            raise
        # 유료 응답을 먼저 저장해 로컬 검증 실패만으로 재호출하지 않게 한다.
        self.ledger.complete(receipt, result, self.provider.last_receipt)
        return output_type.model_validate(result).model_dump()

    def build_graph(self, key, pack, checkpointer):
        evidence = prompt_evidence(pack)

        def check_plan(plan):
            plan = json.loads(json.dumps(plan))
            errors, contracts = validate_plan(plan, pack), []
            if not errors:
                for i, action in enumerate(plan["actions"]):
                    try:
                        contract = compile_measurement(action, pack)
                        contracts.append({"action_index": i, **contract})
                        action["measurement"].update(render_measurement(contract))
                    except ValueError as exc:
                        errors.append(f"actions[{i}].measurement: {exc}")
            if not errors and plan["actions"]:
                plan["monitoring_plan"] = "\n\n".join(
                    action["measurement"]["follow_up"] + "\n" + action["measurement"]["positive_signal"]
                    + "\n" + action["measurement"]["stop_or_revise"] for action in plan["actions"]
                )
            return errors, contracts, plan

        def event(state, name): return state.get("events", []) + [name]

        def diagnose(state):
            result = self.call(key, "diagnose", Diagnosis, {"evidence": evidence})
            errors = validate_diagnosis(result, pack)
            if not errors:
                result = render_metric_facts(result, pack)
            return {"diagnosis": result, "validation_errors": errors, "events": event(state, "diagnose")}

        def expand(state):
            records = self.repository.lookup(pack, state["diagnosis"]["evidence_requests"])
            return {"expanded_evidence": records, "events": event(state, "expand_evidence")}

        def design(state):
            result = self.call(key, "design", ActionPlan, {
                "evidence": evidence, "diagnosis": state["diagnosis"],
                "requested_evidence": state.get("expanded_evidence", []),
            })
            return {"plan": result, "events": event(state, "design")}

        def validate(state):
            errors, contracts, plan = check_plan(state["plan"])
            return {"plan": plan, "validation_errors": errors, "execution_contracts": contracts,
                    "events": event(state, "validate")}

        def review(state):
            result = self.call(key, "review", Review, {"evidence": evidence, "candidate": state["plan"],
                                                       "execution_contracts": state.get("execution_contracts", [])})
            return {"review": result, "review_errors": validate_review(result, pack),
                    "events": event(state, "review")}

        def repair_failure(state, stage, error):
            # 실패했거나 사용 처리한 슬롯은 비용을 유지하고 미사용 예약만 해제한다.
            for receipt in state.get("repair_slots", {}).values():
                self.ledger.release(receipt)
            return {
                "execution_error": {"stage": stage, "error_type": type(error).__name__},
                "events": event(state, f"{stage}_failed"),
            }

        def reserve_repair(state):
            try:
                slots = self.ledger.reserve_pair(key)
            except BudgetExceeded as exc:
                # remaining() 확인 뒤 다른 실행이 예산을 쓸 수 있다.
                return repair_failure(state, "reserve_repair", exc)
            return {"repair_slots": slots, "events": event(state, "reserve_repair")}

        def revise(state):
            try:
                result = self.call(key, "revise", ActionPlan, {
                    "evidence": evidence, "candidate": state["plan"],
                    "execution_contracts": state.get("execution_contracts", []),
                    "validation_errors": state.get("validation_errors", []),
                    "review_errors": state.get("review_errors", []), "review": state.get("review"),
                }, state["repair_slots"]["revise"])
                errors, contracts, result = check_plan(result)
            except Exception as exc:
                return repair_failure(state, "revise", exc)
            # 이전 검토 결과를 수정된 계획에 재사용하지 않는다.
            return {"plan": result, "validation_errors": errors, "execution_contracts": contracts, "review": None,
                    "review_errors": [], "events": event(state, "revise")}

        def re_review(state):
            try:
                result = self.call(key, "re_review", Review, {"evidence": evidence, "candidate": state["plan"],
                                                             "execution_contracts": state.get("execution_contracts", [])},
                                   state["repair_slots"]["re_review"])
                errors = validate_review(result, pack)
            except Exception as exc:
                return repair_failure(state, "re_review", exc)
            return {"review": result, "review_errors": errors, "events": event(state, "re_review")}

        def finalize(state):
            approved = (not state.get("validation_errors") and not state.get("review_errors")
                        and not state.get("execution_error") and state.get("review")
                        and Review.model_validate(state["review"]).passes())
            return {"status": "completed" if approved else "review_required",
                    "decision": state["plan"]["decision"] if approved else "review_required",
                    "events": event(state, "finalize")}

        def can_repair(state):
            return "reserve_repair" if self.ledger.remaining(key) >= 2 else "finalize"

        builder = StateGraph(AgentState)
        for name, node in [("diagnose", diagnose), ("expand", expand), ("design", design),
                           ("validate", validate), ("review", review), ("reserve_repair", reserve_repair),
                           ("revise", revise), ("re_review", re_review), ("finalize", finalize)]:
            builder.add_node(name, node)
        builder.add_edge(START, "diagnose")
        builder.add_conditional_edges("diagnose", lambda s: "finalize" if s["validation_errors"] else "expand")
        builder.add_edge("expand", "design")
        builder.add_edge("design", "validate")
        builder.add_conditional_edges("validate", lambda s: can_repair(s) if s["validation_errors"] else "review")
        builder.add_conditional_edges("review", lambda s: "finalize" if (
            not s.get("review_errors") and Review.model_validate(s["review"]).passes()
        ) else can_repair(s))
        builder.add_conditional_edges("reserve_repair", lambda s: "finalize" if s.get("execution_error") else "revise")
        builder.add_conditional_edges("revise", lambda s: "finalize" if (
            s.get("execution_error") or s["validation_errors"]
        ) else "re_review")
        builder.add_edge("re_review", "finalize")
        builder.add_edge("finalize", END)
        return builder.compile(checkpointer=checkpointer)

    def run(self, topic, snapshot="SNAP-2", context=None):
        pack = self.repository.build(topic, snapshot, context)
        key = self.key(pack)
        with self._run_lock(key):
            run_dir = self.state_dir / "runs" / key
            run_dir.mkdir(parents=True, exist_ok=True)
            output = run_dir / "result.json"
            if output.exists():
                cached = json.loads(output.read_text())
                cached["cache_hit"] = True
                return cached
            (run_dir / "evidence.json").write_text(json.dumps(pack, ensure_ascii=False, indent=2))
            with SqliteSaver.from_conn_string(str(self.state_dir / "checkpoints.sqlite")) as cp:
                graph = self.build_graph(key, pack, cp)
                config = {"configurable": {"thread_id": key}, "recursion_limit": 30}
                snapshot_state = graph.get_state(config)
                try:
                    if snapshot_state.next:
                        state = graph.invoke(None, config)
                    elif snapshot_state.values:
                        state = snapshot_state.values
                    else:
                        state = graph.invoke({"events": [], "validation_errors": [], "review_errors": []}, config)
                finally:
                    # 시작하지 않은 쌍 예약만 해제하고 실행 여부가 불확실한 비용은 유지한다.
                    current = graph.get_state(config).values or {}
                    if not graph.get_state(config).next:
                        for receipt in current.get("repair_slots", {}).values():
                            try: self.ledger.release(receipt)
                            except ValueError: pass
            result = {
                "run_id": key, "scope": pack["scope"], "model": self.provider.model,
                "prompt_version": prompts.VERSION, "cache_hit": False,
                "status": state["status"], "decision": state["decision"],
                "diagnosis": state.get("diagnosis"), "plan": state.get("plan"),
                "execution_contracts": state.get("execution_contracts", []),
                "provider": getattr(self.provider, "cache_identity", type(self.provider).__name__),
                "review": state.get("review"), "validation_errors": state.get("validation_errors", []),
                "review_errors": state.get("review_errors", []),
                "execution_error": state.get("execution_error"),
                "events": state["events"], "evidence_quality": pack["quality"],
                "metrics": pack["metrics"], "evidence_path": str(run_dir / "evidence.json"),
                "usage": self.ledger.stats(key),
            }
            temporary = output.with_suffix(".tmp")
            temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            temporary.replace(output)
            return result
