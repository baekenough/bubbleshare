import type { Contract, ContractMetric } from './types';

const PHASE_LABELS: Record<string, string> = {
  input: '테스트 입력 준비',
  field_mapping: '필드 대응 확인',
  normal_submission: '정상 제출 확인',
  duplicate_submission: '중복 제출 확인',
  missing_required_field: '필수 항목 누락 확인',
  reconcile: '입력과 결과 대조',
};

type IntegrationStep = {
  phase: string;
  input_example: string;
  operation: string;
  expected_result: string;
  verification_artifact: string;
};
type IntegrationProcedure = {
  verified_route: string;
  verification_ref: string;
  prerequisites_unknown: string[];
  steps: IntegrationStep[];
  release_checks: string[];
};

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function strings(value: unknown): value is string[] {
  return Array.isArray(value) && value.every(item => typeof item === 'string');
}

function isIntegrationStep(value: unknown): value is IntegrationStep {
  const step = record(value);
  return !!step && ['phase', 'input_example', 'operation', 'expected_result', 'verification_artifact']
    .every(key => typeof step[key] === 'string');
}

function isIntegrationProcedure(value: unknown): value is IntegrationProcedure {
  const procedure = record(value);
  return !!procedure && typeof procedure.verified_route === 'string'
    && typeof procedure.verification_ref === 'string'
    && strings(procedure.prerequisites_unknown)
    && Array.isArray(procedure.steps) && procedure.steps.every(isIntegrationStep)
    && strings(procedure.release_checks);
}

function metricLabel(metric: ContractMetric) {
  const parts = metric.metric_id.split('.');
  const labels: Record<string, string> = {
    target_mentions: '브랜드 언급',
    target_recommendations: '브랜드 추천',
    target_owned_citations: '자사 출처 인용',
    target_cited_without_mention: '브랜드 언급 없이 자사 출처 인용',
    mentions: '브랜드 언급',
    recommendations: '브랜드 추천',
    owned_citations: '해당 브랜드 출처 인용',
    cited_without_mention: '브랜드 언급 없이 해당 브랜드 출처 인용',
    citation_responses: '선택 출처를 인용한 응답',
  };
  const label = labels[parts.at(-1) || ''] || metric.description || '관측 지표';
  return parts[0] === 'brand' ? `${label} · ${parts[1]}` : label;
}

function samplingQueries(contract: Contract) {
  const queries = new Map<string, string>();
  const plan = record(contract)?.sampling_plan;
  if (Array.isArray(plan)) {
    for (const item of plan) {
      const row = record(item);
      if (row && typeof row.question_id === 'string' && typeof row.query === 'string') {
        queries.set(row.question_id, row.query);
      }
    }
  }
  return queries;
}

function MetricScope({ metric, queries }: { metric: ContractMetric; queries: Map<string, string> }) {
  const groups = new Map<string, string[]>();
  for (const pair of metric.question_channel_pairs) {
    const channels = groups.get(pair.question_id) || [];
    channels.push(pair.channel);
    groups.set(pair.question_id, channels);
  }
  return <div className="measurement-scope">
    {Array.from(groups, ([question, channels]) => <p key={question}>
      <span>{queries.get(question) || `질문 ${question}`}</span><br />
      <span>{channels.join(' · ')}</span>
    </p>)}
  </div>;
}

export function ContractView({ contract }: { contract: Contract }) {
  const queries = samplingQueries(contract);
  const source = contract.source_citation_check;
  const contextOnly = contract.status === 'context_baseline_only';
  const visibleContract = contextOnly ? {
    status: contract.status,
    action_index: contract.action_index,
    primary_metrics: contract.primary_metrics,
    secondary_metrics: contract.secondary_metrics,
  } : contract;
  const sourceStatus: Record<string, string> = {
    ready: '선택한 자산 확인',
    pending_asset_selection_or_registration: '최종 자산 선택·등록 대기',
  };
  return <div className="contract">
    {contextOnly ? <>
      <p className="contract-cadence">현재 관측값 · 문제를 이해하기 위한 참고 기준선</p>
      <p className="small muted">내부 작업의 완료 기준과 검증 기록으로 작업 완료를 판단합니다. 노출 지표는 현상 파악용이며 작업 효과를 나타내지 않습니다. 게시 일정과 게시 자산 등록 조건은 이 작업에 적용되지 않습니다.</p>
    </> : <>
      <p className="contract-cadence">게시 후 <strong>{contract.cadence.days_after_publication.join('일 · ')}일</strong>에 각 1회 확인합니다.</p>
      <p className="small muted">회차당 질문·채널 {contract.cadence.pairs_per_round}쌍 · 예정 수집 {contract.cadence.total_scheduled_observations}회</p>
    </>}
    {(['primary_metrics', 'secondary_metrics'] as const).map(group => <div key={group}>
      <h5>{group === 'primary_metrics' ? (contextOnly ? '관련 지표 · 현재 기준선' : '주요 지표 · 개선 판단') : '보조 지표 · 참고 관측'}</h5>
      {contract[group].length === 0 ? <p className="muted small">선택한 보조 지표가 없습니다.</p> : contract[group].map(metric => <div className="contract-metric" key={metric.metric_id}>
        <div><span>{metricLabel(metric)}</span><strong>현재 {metric.numerator}/{metric.denominator} 응답</strong></div>
        <MetricScope metric={metric} queries={queries} />
      </div>)}
    </div>)}
    {!contextOnly && <>
      <h5>발행 자산의 후속 인용 확인</h5>
      <p className="small">{sourceStatus[source.status] || source.status}{source.source_ids.length === 0 && ' · 신규 자산 등록 필요'}</p>
      <p className="small muted preserve">{source.registration_rule}</p>
    </>}
    <details className="secondary-detail"><summary>{contextOnly ? '참고 기준선과 내부 ID' : '측정 범위와 내부 ID 원문'}</summary><div className="detail-body"><pre className="draft-content">{JSON.stringify(visibleContract, null, 2)}</pre></div></details>
  </div>;
}

export function IntegrationView({ procedure }: { procedure: unknown }) {
  if (procedure == null) return null;
  if (!isIntegrationProcedure(procedure)) {
    return <div className="integration-procedure"><p className="small muted">연동 절차의 구조를 확인할 수 없습니다. 아래 원문을 확인해 주세요.</p><details className="secondary-detail"><summary>연동 절차 원문 확인</summary><div className="detail-body"><pre className="draft-content">{JSON.stringify(procedure, null, 2)}</pre></div></details></div>;
  }
  return <div className="integration-procedure">
    <p><strong>확인된 연동 경로</strong><br />{procedure.verified_route}</p>
    {procedure.prerequisites_unknown.length > 0 && <div><h5>실행 전 확인할 사항</h5><ul>{procedure.prerequisites_unknown.map((item, i) => <li key={i}>{item}</li>)}</ul></div>}
    <ol className="steps integration-steps">{procedure.steps.map((step, i) => <li key={i}>
      <h5>{PHASE_LABELS[step.phase] || step.phase}</h5>
      <dl className="integration-fields">
        <dt>테스트 입력</dt><dd className="preserve">{step.input_example}</dd>
        <dt>실행할 작업</dt><dd className="preserve">{step.operation}</dd>
        <dt>결과 판정 기준</dt><dd className="preserve">{step.expected_result}</dd>
        <dt>남길 검증 기록</dt><dd className="preserve">{step.verification_artifact}</dd>
      </dl>
    </li>)}</ol>
    {procedure.release_checks.length > 0 && <div><h5>발행 전 확인</h5><ul>{procedure.release_checks.map((item, i) => <li key={i}>{item}</li>)}</ul></div>}
    <details className="secondary-detail"><summary>확인 근거 ID와 연동 절차 원문</summary><div className="detail-body"><p className="small">확인 근거 <code>{procedure.verification_ref}</code></p><pre className="draft-content">{JSON.stringify(procedure, null, 2)}</pre></div></details>
  </div>;
}
