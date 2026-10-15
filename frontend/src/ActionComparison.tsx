import type { ActionCandidate, ActionSelection } from './types';

const DISPOSITION: Record<ActionCandidate['disposition'], string> = {
  selected: '선정', deferred: '보류', rejected: '배제',
};
const EFFORT: Record<ActionCandidate['effort']['size'], string> = {
  small: '작음', medium: '보통', large: '큼',
};
const FEASIBILITY: Record<ActionCandidate['feasibility'], string> = {
  ready: '착수 가능', needs_verification: '사전 확인 필요', blocked: '선행 문제 해결 필요',
};

export function ActionComparison({ selection }: { selection?: ActionSelection | null }) {
  if (!selection || selection.candidates.length === 0) return null;
  return <section className="action-comparison" aria-label="액션 후보 비교와 선정 근거">
    <div className="selection-reason">
      <h4>이 액션을 먼저 고른 이유</h4>
      <p>{selection.selection_reason}</p>
    </div>
    <details className="comparison-disclosure">
      <summary><span>같은 문제의 액션 후보 {selection.candidates.length}개 비교</span><span className="comparison-question"><code>{selection.question_id}</code> · 선정·보류·배제 근거</span></summary>
      <div className="comparison-body">
        <p className="comparison-note">기대 이득은 근거에 따른 가설입니다. 실행 결과로 효과를 확인해야 합니다.</p>
        <div className="candidate-list">
          {selection.candidates.map(candidate => <article className={`candidate-card ${candidate.candidate_id === selection.selected_candidate_id ? 'is-selected' : ''}`} key={candidate.candidate_id}>
            <header><span className={`tag ${candidate.disposition === 'selected' ? 'positive' : candidate.disposition === 'deferred' ? 'amber' : ''}`}>{DISPOSITION[candidate.disposition]}</span><h5>{candidate.title}</h5></header>
            <p className="candidate-decision"><strong>{DISPOSITION[candidate.disposition]} 근거</strong>{candidate.decision_reason}</p>
            <dl className="candidate-criteria">
              <div><dt>효과 가설</dt><dd>{candidate.effect_hypothesis}</dd></div>
              <div><dt>기대 이득</dt><dd>{candidate.expected_benefit}</dd></div>
              <div><dt>예상 투입</dt><dd><strong>{EFFORT[candidate.effort.size]}</strong> · {candidate.effort.assumptions}</dd></div>
              <div><dt>실행 가능성</dt><dd>{FEASIBILITY[candidate.feasibility]}</dd></div>
              <div><dt>선행 조건</dt><dd>{candidate.prerequisites.length > 0 ? <ul>{candidate.prerequisites.map((condition, index) => <li key={index}>{condition}</li>)}</ul> : '추가 선행 조건 없음'}</dd></div>
              <div><dt>위험·한계</dt><dd>{candidate.risk}</dd></div>
              <div><dt>분석 근거 ID</dt><dd><span className="id-list">{candidate.evidence_ids.map(id => <code key={id}>{id}</code>)}</span></dd></div>
            </dl>
          </article>)}
        </div>
        {selection.reversal_conditions.length > 0 && <div className="selection-reversal"><h5>이 조건이면 선정을 다시 검토합니다</h5><ul>{selection.reversal_conditions.map((condition, index) => <li key={index}>{condition}</li>)}</ul></div>}
      </div>
    </details>
  </section>;
}
