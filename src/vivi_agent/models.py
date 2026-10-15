"""출력 구조를 검증하는 타입. 구조 검증 통과만으로 내용 품질을 보장하지 않는다."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fact(StrictModel):
    statement: str
    evidence_ids: list[str]
    metric_ids: list[str]


class Hypothesis(StrictModel):
    statement: str
    supporting_evidence_ids: list[str]
    counterevidence: str
    verification: str


class Diagnosis(StrictModel):
    decision: Literal["recommend", "observe", "needs_data"]
    opportunity: str
    facts: list[Fact]
    hypotheses: list[Hypothesis]
    evidence_requests: list[str]
    limitations: list[str]


class AssetAssessment(StrictModel):
    source_id: str
    reuse_decision: Literal["inspect_for_reuse", "supporting_only", "exclude_for_this_task"]
    rationale: str = Field(description="Metadata-grounded task relevance or exclusion reason only. Do not put edit/new-document decisions here; selection_rule owns the complete cross-candidate procedure. Do not claim unseen body content.")


class Asset(StrictModel):
    mode: Literal["revise", "create", "verify_then_choose", "operational"]
    source_ids: list[str] = Field(description="Only editable target-brand owned SRC identifiers from evidence.sources; [] for a new asset")
    proposed_title: str
    selection_rule: str
    assessments: list[AssetAssessment] = Field(description="Assess every target-owned source in the supplied inventory, including specialized guides; selected source_ids must equal inspect_for_reuse candidates; supporting_only sources must never be edit or publication candidates")


class Deliverable(StrictModel):
    name: str
    outline: list[str]
    example_content: str = Field(description="A usable draft/table/checklist, not instructions to write one")


class Step(StrictModel):
    instruction: str
    expected_output: str


class Measurement(StrictModel):
    baseline_metric_ids: list[str] = Field(description="Primary success metrics only: current question.* metrics whose full denominator is within this action's question/channel scope")
    secondary_metric_ids: list[str] = Field(description="Optional wider topic/channel metrics for secondary observation only; code expands every metric to its full question/channel sampling scope")
    follow_up: str
    positive_signal: str
    stop_or_revise: str


class IntegrationStep(StrictModel):
    phase: Literal["input", "field_mapping", "normal_submission", "duplicate_submission", "missing_required_field", "reconcile"]
    input_example: str = Field(description="Concrete fictional customer input or preceding-step output; never invent product field identifiers")
    operation: str
    expected_result: str = Field(description="Test acceptance condition, not a claim of verified product behavior; unknown policies must be agreed before running")
    verification_artifact: str = Field(description="What the operator records to verify this step; do not assume a product log feature exists")


class IntegrationProcedure(StrictModel):
    verified_route: str
    verification_ref: str
    prerequisites_unknown: list[str]
    steps: list[IntegrationStep]
    release_checks: list[str]


class ActionBrief(StrictModel):
    title: str
    action_type: str
    question_ids: list[str]
    channels: list[Literal["ChatGPT", "Gemini", "Perplexity"]] = Field(description="Observed AI answer channels to improve, never website/publication channels")
    evidence_ids: list[str]
    opportunity: str
    mechanism_hypothesis: str
    target_asset: Asset
    owner_role: str
    deliverables: list[Deliverable]
    steps: list[Step]
    prerequisites: list[str]
    acceptance_criteria: list[str]
    measurement: Measurement
    integration_procedure: IntegrationProcedure | None = Field(description="Required when context verifies auto-registration via a specific route. Null when unsupported/unknown. Preserve known route and receipt exactly; do not invent UI/API details.")
    priority_reason: str
    effort_assumptions: str
    readiness: Literal["ready_to_start", "needs_input", "blocked"]


class Alternative(StrictModel):
    title: str
    rejection_reason: str


class DataTask(StrictModel):
    question_id: str = Field(description="Question from an actually excluded failed/partial/language_mismatch observation")
    channel: Literal["ChatGPT", "Gemini", "Perplexity"]
    reason: str
    completion_condition: str


class ActionPlan(StrictModel):
    decision: Literal["recommend", "observe", "needs_data"]
    summary: str
    actions: list[ActionBrief] = Field(max_length=1, description="One highest-value actionable intervention, or [] for observe/needs_data")
    rejected_alternatives: list[Alternative]
    monitoring_plan: str
    data_tasks: list[DataTask] = Field(description="Recollect failed/partial observations only. Empty if none missing. Product checks belong in action prerequisites/steps.")
    limitations: list[str]


class Score(StrictModel):
    value: int = Field(ge=0, le=2)
    reason: str


class Scores(StrictModel):
    grounding: Score
    relevance: Score
    specificity: Score
    feasibility: Score
    verification: Score


class ReviewIssue(StrictModel):
    path: str
    problem: str
    required_change: str


class ClaimCheck(StrictModel):
    quote: str
    subject: str
    status: Literal["verified", "unverified", "not_a_product_claim"]
    asserted_as_fact: bool
    verification_ref: str | None = Field(description="Exact receipt_id from business_context.verified_product_facts or business_context.evidence_receipt; null when no verification exists")


class Review(StrictModel):
    verdict: Literal["approve", "revise", "reject"]
    scores: Scores
    hard_violations: list[str]
    issues: list[ReviewIssue]
    claim_checks: list[ClaimCheck]
    rationale: str

    def passes(self) -> bool:
        values = [s.value for s in self.scores.__dict__.values()]
        unsafe_claim = any(c.status == "unverified" and c.asserted_as_fact for c in self.claim_checks)
        return (self.verdict == "approve" and not unsafe_claim and not self.hard_violations
                and not self.issues and min(values) >= 1 and sum(values) >= 8)


class CaseEvaluation(StrictModel):
    meets_expectations: bool
    satisfied: list[str]
    missed: list[str]
    forbidden_claims: list[str]
    rationale: str
