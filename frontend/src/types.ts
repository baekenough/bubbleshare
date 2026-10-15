export type Status = 'draft' | 'in_progress' | 'done' | 'dismissed';
export type Topic = {
  topic_id: string;
  name: string;
  coverage: number | null;
  mention_rate: number | null;
  recommendation_rate: number | null;
  priority_score: number;
  priority_reason: string;
};
export type Priority = { question_id: string; score: number; metric_id: string; importance: number; coverage: number; recommendation_gap: number; reason: string }[];
export type Metric = { numerator: number; denominator: number; description?: string };
export type Source = { source_id: string; title: string; url: string; domain: string; is_brand_owned: boolean; brand_id: string | null };
export type Observation = {
  observation_id: string; question_id: string; channel: string; collected_at: string;
  response_text: string; collection_status: string; response_language: string;
  recomputed?: { target_mentioned: boolean };
  citations: { citation_id: string; source_id: string; source: Source }[];
};
export type Evidence = {
  scope: { topic_id: string; snapshot_id: string; target_brand_id: string };
  dataset_hash: string;
  questions: { question_id: string; query: string; topic_label: string }[];
  metrics: Record<string, Metric>;
  observations: Observation[];
  excluded: (Observation & { reason: string })[];
  sources: Source[];
  quality: { eligible: number; expected: number; coverage: number | null };
  capabilities: { body_access: boolean };
};
export type ActionBrief = {
  title: string; action_type: string; question_ids: string[]; channels: string[]; evidence_ids: string[];
  opportunity: string; mechanism_hypothesis: string;
  target_asset: { mode: string; source_ids: string[]; proposed_title: string; selection_rule: string };
  owner_role: string; deliverables: { name: string; outline: string[]; example_content: string }[];
  steps: { instruction: string; expected_output: string }[];
  prerequisites: string[]; acceptance_criteria: string[];
  measurement: { baseline_metric_ids: string[]; secondary_metric_ids?: string[]; follow_up: string; positive_signal: string; stop_or_revise: string };
  priority_reason: string; effort_assumptions: string; readiness: string;
  integration_procedure?: unknown;
};
export type ActionCandidate = {
  candidate_id: string;
  title: string;
  intervention_type: string;
  effect_hypothesis: string;
  evidence_ids: string[];
  expected_benefit: string;
  effort: { size: 'small' | 'medium' | 'large'; assumptions: string };
  feasibility: 'ready' | 'needs_verification' | 'blocked';
  prerequisites: string[];
  risk: string;
  disposition: 'selected' | 'deferred' | 'rejected';
  decision_reason: string;
};
export type ActionSelection = {
  question_id: string;
  candidates: ActionCandidate[];
  selected_candidate_id: string;
  selection_reason: string;
  reversal_conditions: string[];
};
export type Plan = {
  decision: string; summary: string; actions: ActionBrief[]; monitoring_plan: string;
  rejected_alternatives: { title: string; rejection_reason: string }[];
  data_tasks: { question_id: string; channel: string; reason: string; completion_condition: string }[];
  limitations: string[];
  action_selection?: ActionSelection | null;
};
export type ContractMetric = Metric & { metric_id: string; question_channel_pairs: { question_id: string; channel: string }[] };
export type Contract = {
  action_index?: number; status?: string; primary_metrics: ContractMetric[]; secondary_metrics: ContractMetric[];
  cadence: { days_after_publication: number[]; pairs_per_round: number; total_scheduled_observations: number };
  source_citation_check: { source_ids: string[]; status: string; registration_rule: string };
};
export type Proposal = {
  proposal_id?: string | null; result_type: 'propose' | 'cache_hit' | 'no_action' | 'insufficient_evidence' | 'fallback';
  topic_id: string; dataset_version: string; plan: Plan | null; execution_contracts: Contract[];
  priority: Priority; reason: string; requires_human_review: boolean;
};
export type SavedAction = {
  id: string; proposal_id: string; topic_id: string; title: string; notes: string; status: Status;
  created_at: string; updated_at: string; plan: Plan; execution_contracts: Contract[]; dataset_version: string;
};
