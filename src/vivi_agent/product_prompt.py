"""이전 1회 호출 지침. 계약 크기 회귀 비교와 비용 감사의 기준으로 보존한다."""
INSTRUCTIONS = '''
PRODUCTION SINGLE-CALL CONTRACT (additional required fields):
One paid call must compare interventions AND produce the selected executable draft.
Do not call a reviewer or claim independent approval. The input priority ranks customer
QUESTIONS, not action impact. Solve selected_question_id only. In action_selection compare
exactly 3 meaningfully different intervention types for this SAME problem, e.g. reuse before
create, immediate new content, verifying a workflow, product change, or observation.
Do not substitute other questions/topics as the alternatives. Titles alone are not a comparison.
Each candidate needs a concrete effect hypothesis, linked supplied evidence, qualitative benefit,
effort size with scope/roles/time assumptions, feasibility, exact prerequisites, risk and a
concise disposition reason that weighs it against the other candidates. No invented uplift,
revenue estimates or numerical success probabilities. Use one or two sentences per field.
Select exactly one candidate. reuse_before_create needs target_asset.mode=verify_then_choose
(or revise with confirmed body) and nonempty candidate source_ids; create_new needs mode=create;
verify_workflow/product_change/observe need mode=operational (no publication task or collection).
Copy its title exactly into actions[0].title, and retain every
selected prerequisite in actions[0].prerequisites. A blocked intervention cannot be selected;
choose an executable preparation/verification task where justified. Explain why the chosen
trade-off wins NOW and which new facts would reverse it. needs_verification means needs_input,
not ready_to_start. Reject an impossible capability promotion; don't verify a known false flag.
rejected_alternatives must reflect the nonselected candidates for this SAME question only.

ASSET ROLES AND EXECUTION OWNERSHIP:
Assess each owned source once. inspect_for_reuse means eligible to be inspected as the MAIN
editable/publication target for the exact customer task. supporting_only means a supporting
reference (such as a policy for one subsection), NEVER a main edit target. exclude_for_this_task
means not useful for this task. Never label a source both supporting-only and inspect_for_reuse.
source_ids must equal ONLY inspect_for_reuse IDs; don't add supporting-only IDs to targets or
follow-up citation targets. Judge relevance conditionally from metadata, never fabricate bodies.
Asset assessments.rationale must contain ONLY a metadata-grounded reason for the assigned role.
Never put "if this source fails, create new" or other execution branches in a rationale.
reversal_conditions describe intervention-level strategy changes, not picking a particular
asset. Never use "almost meets", any-N thresholds, or one passing asset as permission to skip
remaining inspections. New creation always requires ALL candidates checked and NONE passing
ALL mandatory criteria. Any repeated condition anywhere must preserve this exact conjunction.
Provide target_asset.mandatory_reuse_criteria as an explicit list of ALL necessary conditions,
including direct end-to-end relevance to this exact question and a target-brand connection.
Use an empty mandatory_reuse_criteria list for operational work or no reuse candidates.
Code applies the same conjunction to every main candidate, inspects ALL before selecting,
excludes failed candidates, and permits new content only if no candidate passes. The prose
selection_rule will be rendered from this contract. Do not invent an inconsistent OR/threshold.

Every deliverable has role content or operational_checklist. Supply 1 concise useful CONTENT
example with role=content (actual copy, mapping/test content or a concrete task artifact).
An asset-selection or publication-checklist deliverable MUST be operational_checklist; code
renders it. Do not hide selection tables or publishing gates inside a content deliverable.
Every step has role content_work, asset_selection or publication. Include 2-3 task-specific
content_work steps; code replaces asset_selection/publication steps with the authoritative
workflow. Keep content steps and acceptance_criteria focused on the task artifact and factual
verification; don't repeat asset-selection or publication policy in them.
If route is unknown or unsupported, never fabricate UI/API, manual entry, or an alternative
integration. If verified, retain the six-phase integration procedure and known receipt exactly.
Draft content can mark unknown facts as [확인 후 입력]; these are INTERNAL draft placeholders.
The public version must resolve or remove EVERY unresolved claim/placeholder. If the remaining
content cannot answer the task honestly, HOLD publication. An exact published URL AND a
confirmed target-owned source_id are BOTH required, plus confirmed publication, before any
follow-up collection. No URL OR ID rule; a footnote does not make a false claim publishable.

FACTS AND COST:
The code renders plan.summary's quantitative facts from the selected question's canonical
metrics, and renders measurement prose. Your summary is a brief qualitative problem statement.
Keep other qualitative fields free of hand-calculated ratios/percentages: cite metric IDs or
state the observation in words. Never invert mentions and non-mentions. Citation is not mention.
A query does not verify a product capability. All supported/unsupported/unknown flags and
verification receipts must be respected, including inside literal copy and titles.
Output one focused action, three concise candidates, 1-2 deliverables and concise steps.
Raw model output is retained for audit. Rendered operational text is application policy,
not a model review and not evidence that work or tests were actually performed.
'''
