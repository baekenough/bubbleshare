import { useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { api, errorMessage } from './api';
import { ContractView, IntegrationView } from './ExecutionDetails';
import { ActionComparison } from './ActionComparison';
import type { ActionBrief, Contract, Evidence, Metric, Plan, Priority, Proposal, SavedAction, Status, Topic } from './types';

const STATUS_LABELS: Record<Status, string> = { draft: '검토 중', in_progress: '진행 중', done: '완료', dismissed: '보류' };
const RESULT_LABELS: Record<Proposal['result_type'], string> = {
  propose: '새 제안', cache_hit: '이전 초안 재사용', no_action: '추가 액션 없음',
  insufficient_evidence: '근거 보완 필요', fallback: '생성 결과 확인 필요',
};
const percent = (value: number | null | undefined) => value == null ? '—' : `${Math.round(value * 100)}%`;
const ratio = (metric: Metric | undefined) => metric ? `${metric.numerator}/${metric.denominator}` : '—';
const date = (value: string) => Number.isNaN(Date.parse(value)) ? value : new Intl.DateTimeFormat('ko-KR', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value));

function Loading({ children = '불러오는 중입니다.' }: { children?: ReactNode }) {
  return <div className="loading" role="status"><span className="spinner" aria-hidden="true" />{children}</div>;
}
function ErrorBox({ message, retry }: { message: string; retry?: () => void }) {
  return <div className="error-box" role="alert"><span>{message}</span>{retry && <button className="text-button" onClick={retry}>다시 시도</button>}</div>;
}
function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return <div className="empty"><span className="empty-mark" aria-hidden="true">↗</span><h3>{title}</h3>{children && <p>{children}</p>}</div>;
}
function Ids({ values }: { values: string[] }) {
  return <span className="id-list">{values.map(value => <code key={value}>{value}</code>)}</span>;
}
function Block({ title, children }: { title: string; children: ReactNode }) {
  return <section className="brief-block"><h4>{title}</h4>{children}</section>;
}

function ActionSummary({ action }: { action: ActionBrief }) {
  const firstStep = action.steps[0];
  return <article className="action-summary" aria-label="추천하는 다음 액션">
    <h3 className="action-title">{action.title}</h3>
    <div className="brief-meta"><span className="tag">담당 · {action.owner_role}</span><span className={`tag ${action.readiness === 'ready_to_start' ? 'positive' : 'amber'}`}>{action.readiness === 'ready_to_start' ? '시작 가능' : action.readiness === 'blocked' ? '선행 문제 해결 필요' : '사전 확인 필요'}</span></div>
    <dl className="action-facts">
      <div><dt>추천 이유</dt><dd>{action.opportunity}</dd></div>
      {firstStep && <><div className="first-step"><dt>지금 할 첫 단계</dt><dd>{firstStep.instruction}</dd></div><div><dt>첫 단계의 산출물</dt><dd>{firstStep.expected_output}</dd></div></>}
    </dl>
    {action.prerequisites.length > 0 && <p className="prerequisite-note"><strong>시작 전 확인</strong> {action.prerequisites[0]}{action.prerequisites.length > 1 && <span className="muted"> · 나머지 {action.prerequisites.length - 1}건은 실행 계획에서 확인</span>}</p>}
  </article>;
}

function BriefView({ action, contract }: { action: ActionBrief; contract?: Contract }) {
  return <div className="brief">
    <div className="brief-meta"><span className="tag">{action.owner_role}</span><span className={`tag ${action.readiness === 'ready_to_start' ? 'positive' : 'amber'}`}>{action.readiness === 'ready_to_start' ? '시작 가능' : action.readiness === 'blocked' ? '선행 문제 해결 필요' : '사전 확인 필요'}</span></div>
    <Block title="왜 이 액션인가"><p>{action.opportunity}</p><p className="muted">{action.priority_reason}</p></Block>
    <Block title="적용 범위"><Ids values={action.question_ids} /><span className="small inline-muted">{action.channels.join(' · ')}</span></Block>
    <Block title={action.target_asset.mode === 'operational' ? '작업 대상' : '변경할 자산'}><strong>{action.target_asset.proposed_title}</strong><p><Ids values={action.target_asset.source_ids} />{action.target_asset.source_ids.length === 0 && <span className="muted">{action.target_asset.mode === 'operational' ? '내부 작업 · 게시 자산 등록 대상 아님' : '신규 자산 · 발행 전 source ID 등록 필요'}</span>}</p><p>{action.target_asset.selection_rule}</p></Block>
    {action.prerequisites.length > 0 && <Block title="먼저 확인할 것"><ul>{action.prerequisites.map((item, i) => <li key={i}>{item}</li>)}</ul></Block>}
    <Block title="실행 순서"><ol className="steps">{action.steps.map((step, i) => <li key={i}><p>{step.instruction}</p><span>산출물 · {step.expected_output}</span></li>)}</ol></Block>
    <Block title="작업 초안">{action.deliverables.map((deliverable, i) => <details className="draft-detail" key={i} open={i === 0}><summary>{deliverable.name}</summary><div className="detail-body"><ul className="muted small">{deliverable.outline.map((line, index) => <li key={index}>{line}</li>)}</ul><pre className="draft-content">{deliverable.example_content}</pre></div></details>)}</Block>
    {!!action.integration_procedure && <Block title="연동 검증 절차"><IntegrationView procedure={action.integration_procedure} /></Block>}
    <Block title="완료 기준"><ul>{action.acceptance_criteria.map((item, i) => <li key={i}>{item}</li>)}</ul></Block>
    <Block title="변화를 어떻게 확인하나">{contract && <ContractView contract={contract} />}<p className="preserve">{action.measurement.follow_up}</p><h5>긍정 신호</h5><p>{action.measurement.positive_signal}</p><h5>중단 또는 재검토</h5><p>{action.measurement.stop_or_revise}</p></Block>
    <details className="secondary-detail"><summary>가설·투입 가정·근거 ID</summary><div className="detail-body"><p>{action.mechanism_hypothesis}</p><p className="muted">{action.effort_assumptions}</p><Ids values={action.evidence_ids} /></div></details>
  </div>;
}

function PlanView({ plan, contracts }: { plan: Plan; contracts: Contract[] }) {
  const details = <>
    <p className="plan-summary">{plan.summary}</p>
    {plan.actions.map((action, i) => <BriefView key={i} action={action} contract={contracts.find(contract => (contract.action_index ?? 0) === i)} />)}
    {plan.actions.length === 0 && plan.monitoring_plan && <Block title="다음 확인"><p className="preserve">{plan.monitoring_plan}</p></Block>}
    {plan.data_tasks.length > 0 && <Block title="추가 수집할 근거"><ul>{plan.data_tasks.map((task, i) => <li key={i}><strong>{task.question_id} / {task.channel}</strong> — {task.reason}<p className="small muted">{task.completion_condition}</p></li>)}</ul></Block>}
    {plan.limitations.length > 0 && <Block title="판단의 한계"><ul className="muted small">{plan.limitations.map((item, i) => <li key={i}>{item}</li>)}</ul></Block>}
    {plan.rejected_alternatives.length > 0 && <details className="secondary-detail"><summary>다른 액션을 선택하지 않은 이유</summary><div className="detail-body">{plan.rejected_alternatives.map((alternative, i) => <p key={i}><strong>{alternative.title}</strong><br />{alternative.rejection_reason}</p>)}</div></details>}
  </>;
  return <div>
    {plan.actions.map((action, i) => <ActionSummary key={i} action={action} />)}
    <ActionComparison selection={plan.action_selection} />
    {plan.actions.length > 0 ? <details className="execution-plan"><summary>추천 근거·전체 실행 계획·측정 기준 검토 <span>펼쳐보기</span></summary><div className="detail-body">{details}</div></details> : details}
  </div>;
}

function EvidenceView({ evidence }: { evidence: Evidence }) {
  const [tab, setTab] = useState<'questions' | 'sources'>('questions');
  const [channel, setChannel] = useState('all');
  const channels = Array.from(new Set(evidence.observations.map(row => row.channel))).sort();
  return <>
    <div className="metrics-row">
      {[['브랜드 언급', 'topic.target_mentions'], ['브랜드 추천', 'topic.target_recommendations'], ['자사 출처 인용', 'topic.target_owned_citations']].map(([label, key]) => <div className="metric-card" key={key}><span>{label}</span><strong>{ratio(evidence.metrics[key])}<small>응답</small></strong><p>적격 응답 기준</p></div>)}
    </div>
    <div className="coverage-note"><span className="status-dot" />수집 커버리지 {percent(evidence.quality.coverage)} · {evidence.quality.expected}개 중 {evidence.quality.eligible}개 적격<span>불완전 응답은 미노출로 계산하지 않습니다.</span></div>
    <div className="subnav"><div><button aria-pressed={tab === 'questions'} onClick={() => setTab('questions')}>질문과 원문 응답</button><button aria-pressed={tab === 'sources'} onClick={() => setTab('sources')}>인용 출처</button></div>{tab === 'questions' && <select aria-label="응답 채널 필터" value={channel} onChange={event => setChannel(event.target.value)}><option value="all">모든 AI 채널</option>{channels.map(value => <option key={value}>{value}</option>)}</select>}</div>
    {tab === 'questions' ? <div className="questions">
      {evidence.questions.length === 0 && <Empty title="조회할 질문이 없습니다." />}
      {evidence.questions.map(question => {
        const observations = evidence.observations.filter(row => row.question_id === question.question_id && (channel === 'all' || row.channel === channel));
        return <details className="question" key={question.question_id}><summary><code>{question.question_id}</code><span>{question.query}</span><small>{observations.length}개 응답</small></summary><div className="question-body">
          {observations.length === 0 && <p className="muted">이 범위에는 적격 응답이 없습니다.</p>}
          {observations.map(row => <article className="response" key={row.observation_id}><header><strong>{row.channel}</strong><code>{row.observation_id}</code>{row.recomputed?.target_mentioned && <span className="tag positive">브랜드 언급</span>}</header><p className="response-text">{row.response_text}</p><div className="response-sources">{row.citations.length > 0 ? row.citations.map(citation => <span key={citation.citation_id}><code>{citation.source_id}</code>{citation.source?.title || citation.citation_id}</span>) : <span>인용된 출처가 없습니다.</span>}</div><small className="muted">수집 {date(row.collected_at)} · {row.response_language}</small></article>)}
        </div></details>;
      })}
      {evidence.excluded.length > 0 && <details className="secondary-detail"><summary>분모에서 제외된 응답 {evidence.excluded.length}건</summary><ul className="detail-body small">{evidence.excluded.map(row => <li key={row.observation_id}><code>{row.observation_id}</code> · {row.question_id}/{row.channel} · {({ partial: '부분 수집', failed: '수집 실패', language_mismatch: '언어 불일치', superseded: '재수집으로 대체' } as Record<string, string>)[row.reason] || row.reason}</li>)}</ul></details>}
    </div> : <div className="sources-list"><p className="muted small">제공 데이터에 기록된 출처 메타데이터입니다. 본문 확인 여부와 제품 기능 검증은 구분합니다.</p>{evidence.sources.length === 0 ? <Empty title="기록된 출처가 없습니다." /> : evidence.sources.map(source => <article className="source-card" key={source.source_id}><div><code>{source.source_id}</code>{source.is_brand_owned && source.brand_id === evidence.scope.target_brand_id && <span className="tag positive">자사 출처</span>}</div><strong>{source.title}</strong><p>{source.url}</p><span className="muted small">{source.domain} · 본문 {evidence.capabilities.body_access ? '조회 가능' : '미확인'}</span></article>)}</div>}
  </>;
}

function ActionCard({ action, onUpdate }: { action: SavedAction; onUpdate: (row: SavedAction) => void }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(action.title);
  const [notes, setNotes] = useState(action.notes || '');
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function update(fields: { status?: Status; title?: string; notes?: string }) {
    setPending(true); setError('');
    try {
      const row = await api<SavedAction>(`/actions/${encodeURIComponent(action.id)}`, { method: 'PATCH', body: JSON.stringify(fields) });
      onUpdate(row);
      if (mounted.current) { setEditing(false); setTitle(row.title); setNotes(row.notes || ''); }
    } catch (err) { if (mounted.current) setError(errorMessage(err)); }
    finally { if (mounted.current) setPending(false); }
  }
  return <article className="saved-action">
    <header><div><span className="eyebrow">{action.topic_id} · {date(action.updated_at)}</span><h3>{action.title}</h3></div><label className="status-control"><span className="sr-only">{action.title} 상태</span><select value={action.status} disabled={pending || editing} onChange={event => void update({ status: event.target.value as Status })}>{Object.entries(STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label></header>
    {error && <ErrorBox message={error} />}
    {editing ? <form className="edit-form" onSubmit={event => { event.preventDefault(); void update({ title: title.trim(), notes: notes.trim() }); }}><label>액션 제목<input required maxLength={200} value={title} onChange={event => setTitle(event.target.value)} /></label><label>실행 메모<textarea maxLength={4000} rows={3} value={notes} onChange={event => setNotes(event.target.value)} /></label><div className="button-row"><button className="primary-button" disabled={pending || !title.trim()}>{pending ? '저장 중…' : '변경 저장'}</button><button type="button" className="secondary-button" disabled={pending} onClick={() => { setEditing(false); setTitle(action.title); setNotes(action.notes || ''); }}>취소</button></div></form> : <>{action.notes && <p className="action-notes preserve">{action.notes}</p>}<button className="text-button" onClick={() => setEditing(true)} disabled={pending}>제목·메모 수정</button></>}
    {pending && <p className="small muted" role="status">변경 사항을 저장하고 있습니다.</p>}
    <details className="saved-details"><summary>저장한 제안과 근거 보기 <span>데이터 {action.dataset_version.slice(0, 10)}</span></summary><div className="detail-body"><PlanView plan={action.plan} contracts={action.execution_contracts || []} /></div></details>
  </article>;
}

export default function App() {
  const [page, setPage] = useState<'analysis' | 'actions'>('analysis');
  const [topics, setTopics] = useState<Topic[]>([]);
  const [datasetVersion, setDatasetVersion] = useState('');
  const [topicLoading, setTopicLoading] = useState(true);
  const [topicError, setTopicError] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [selected, setSelected] = useState('');
  const [search, setSearch] = useState('');
  const [coverageFilter, setCoverageFilter] = useState('all');
  const [sort, setSort] = useState('priority');
  const [detail, setDetail] = useState<{ evidence: Evidence; priority: Priority } | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [proposing, setProposing] = useState(false);
  const [proposalError, setProposalError] = useState('');
  const proposalController = useRef<AbortController | null>(null);
  const proposalGeneration = useRef(0);
  const [saveTitle, setSaveTitle] = useState('');
  const [saveNotes, setSaveNotes] = useState('');
  const [saving, setSaving] = useState(false);
  const [savedId, setSavedId] = useState('');
  const [saveError, setSaveError] = useState('');
  const [actions, setActions] = useState<SavedAction[]>([]);
  const [actionsLoading, setActionsLoading] = useState(true);
  const [actionsError, setActionsError] = useState('');
  const [actionRefresh, setActionRefresh] = useState(0);
  const [statusFilter, setStatusFilter] = useState('all');

  useEffect(() => {
    const controller = new AbortController();
    setTopicLoading(true); setTopicError('');
    api<{ items: Topic[]; dataset_version: string }>('/topics', { signal: controller.signal })
      .then(data => { if (!controller.signal.aborted) { setTopics(data.items); setDatasetVersion(data.dataset_version); setSelected(previous => data.items.some(item => item.topic_id === previous) ? previous : data.items[0]?.topic_id || ''); } })
      .catch(error => { if (!controller.signal.aborted) setTopicError(errorMessage(error)); })
      .finally(() => { if (!controller.signal.aborted) setTopicLoading(false); });
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    const controller = new AbortController();
    proposalController.current?.abort();
    proposalGeneration.current += 1;
    setDetail(null); setDetailError(''); setProposal(null); setProposing(false); setProposalError('');
    setSavedId(''); setSaveError(''); setSaving(false); setSaveTitle(''); setSaveNotes('');
    if (!selected) { setDetailLoading(false); return () => controller.abort(); }
    setDetailLoading(true);
    api<{ evidence: Evidence; priority: Priority }>(`/topics/${encodeURIComponent(selected)}`, { signal: controller.signal })
      .then(data => { if (!controller.signal.aborted) setDetail(data); })
      .catch(error => { if (!controller.signal.aborted) setDetailError(errorMessage(error)); })
      .finally(() => { if (!controller.signal.aborted) setDetailLoading(false); });
    return () => { controller.abort(); proposalController.current?.abort(); };
  }, [selected, refresh]);

  useEffect(() => {
    const controller = new AbortController();
    setActionsLoading(true); setActionsError('');
    api<{ items: SavedAction[] }>('/actions', { signal: controller.signal })
      .then(data => { if (!controller.signal.aborted) setActions(data.items); })
      .catch(error => { if (!controller.signal.aborted) setActionsError(errorMessage(error)); })
      .finally(() => { if (!controller.signal.aborted) setActionsLoading(false); });
    return () => controller.abort();
  }, [actionRefresh]);

  const filteredTopics = useMemo(() => topics.filter(topic => (`${topic.name} ${topic.topic_id}`).toLocaleLowerCase().includes(search.toLocaleLowerCase())
    && (coverageFilter === 'all' || (coverageFilter === 'complete' ? topic.coverage === 1 : topic.coverage !== 1)))
    .sort((a, b) => sort === 'name' ? a.name.localeCompare(b.name, 'ko') : sort === 'mention' ? (a.mention_rate ?? 2) - (b.mention_rate ?? 2) : b.priority_score - a.priority_score), [topics, search, coverageFilter, sort]);
  const activeTopic = topics.find(topic => topic.topic_id === selected);
  const priorityQuestion = detail?.evidence.questions.find(question => question.question_id === detail.priority[0]?.question_id);
  const priorityMetric = priorityQuestion && detail?.evidence.metrics[`question.${priorityQuestion.question_id}.target_recommendations`];
  const filteredActions = actions.filter(action => statusFilter === 'all' || action.status === statusFilter);

  async function createProposal() {
    proposalController.current?.abort();
    const controller = new AbortController();
    proposalController.current = controller;
    proposalGeneration.current += 1;
    setProposing(true); setProposalError(''); setProposal(null); setSaveError(''); setSavedId(''); setSaveNotes('');
    try {
      const data = await api<Proposal>('/proposals', { method: 'POST', signal: controller.signal, body: JSON.stringify({ topic_id: selected, snapshot_id: 'SNAP-2' }) });
      if (!controller.signal.aborted) { setProposal(data); setSaveTitle(data.plan?.actions[0]?.title || ''); }
    } catch (error) { if (!controller.signal.aborted) setProposalError(errorMessage(error)); }
    finally { if (!controller.signal.aborted) setProposing(false); }
  }
  async function saveProposal(event: FormEvent) {
    event.preventDefault();
    if (!proposal?.proposal_id || !saveTitle.trim()) return;
    const generation = proposalGeneration.current;
    setSaving(true); setSaveError('');
    try {
      const row = await api<SavedAction>('/actions', { method: 'POST', body: JSON.stringify({ proposal_id: proposal.proposal_id, title: saveTitle.trim(), notes: saveNotes.trim() }) });
      setActions(previous => [row, ...previous.filter(item => item.id !== row.id)]);
      if (proposalGeneration.current === generation) setSavedId(row.id);
    } catch (error) { if (proposalGeneration.current === generation) setSaveError(errorMessage(error)); }
    finally { if (proposalGeneration.current === generation) setSaving(false); }
  }
  function updateAction(row: SavedAction) { setActions(previous => previous.map(item => item.id === row.id ? row : item)); }

  return <div className="app-shell">
    <a href="#main-content" className="skip-link">본문으로 이동</a>
    <header className="app-header"><a className="brand" href="#" onClick={event => { event.preventDefault(); setPage('analysis'); }} aria-label="VIVI 분석 홈"><span className="brand-symbol" aria-hidden="true">v</span><span>vivi<span className="brand-dot">.</span></span></a><span className="header-divider" /><span className="workspace-name">액션 워크스페이스</span><nav aria-label="주요 메뉴"><button aria-current={page === 'analysis' ? 'page' : undefined} onClick={() => setPage('analysis')}>다음 액션</button><button aria-current={page === 'actions' ? 'page' : undefined} onClick={() => setPage('actions')}>액션 관리{!actionsLoading && <span className="nav-count">{actions.length}</span>}</button></nav><span className="dataset-badge">제공 데이터 · SNAP-2</span></header>
    <div className="page-heading"><div><span className="eyebrow">AI SEARCH VISIBILITY</span><h1>{page === 'analysis' ? '지금 할 개선 액션을 찾으세요.' : '검토한 제안을 실행으로 옮기세요.'}</h1><p>{page === 'analysis' ? '분석 데이터에서 우선할 일을 찾고, 첫 단계와 산출물까지 제안합니다.' : '저장한 액션과 메모, 진행 상태를 한곳에서 관리합니다.'}</p></div><span className="environment-note"><span className="status-dot" />합성 데이터 분석{datasetVersion && <small title={datasetVersion}>버전 {datasetVersion.slice(0, 10)}</small>}</span></div>
    <main id="main-content">
      {page === 'analysis' ? <div className="analysis-layout">
        <aside className="topic-panel" aria-label="분석 주제 목록"><div className="panel-heading"><h2>분석 주제</h2><span>{topics.length}</span></div><div className="topic-controls"><label className="search-box"><span aria-hidden="true">⌕</span><input aria-label="주제 검색" type="search" placeholder="주제 또는 ID 검색" value={search} onChange={event => setSearch(event.target.value)} /></label><div className="filter-row"><select aria-label="수집 커버리지 필터" value={coverageFilter} onChange={event => setCoverageFilter(event.target.value)}><option value="all">모든 수집 상태</option><option value="complete">전체 수집</option><option value="incomplete">누락 있음</option></select><select aria-label="주제 정렬" value={sort} onChange={event => setSort(event.target.value)}><option value="priority">우선순위순</option><option value="mention">언급률 낮은순</option><option value="name">이름순</option></select></div></div>
          {topicLoading ? <Loading /> : topicError ? <ErrorBox message={topicError} retry={() => setRefresh(value => value + 1)} /> : filteredTopics.length === 0 ? <Empty title="일치하는 주제가 없습니다.">검색어나 수집 상태 필터를 바꿔보세요.</Empty> : <div className="topic-list">{filteredTopics.map(topic => <button className={`topic-item ${selected === topic.topic_id ? 'selected' : ''}`} key={topic.topic_id} aria-pressed={selected === topic.topic_id} onClick={() => setSelected(topic.topic_id)}><div><strong>{topic.name}</strong><span className="priority-pill">우선순위 {Number.isFinite(topic.priority_score) ? Math.round(topic.priority_score) : '—'}</span></div><p>언급 {percent(topic.mention_rate)}<span>추천 {percent(topic.recommendation_rate)}</span></p><div className="topic-bottom"><code>{topic.topic_id}</code><span>수집 {percent(topic.coverage)}</span></div></button>)}</div>}
        </aside>
        <div className="detail-column">
          {!selected && !topicLoading && !topicError ? <section className="panel"><Empty title="분석할 주제가 없습니다.">데이터를 불러온 뒤 다시 확인해 주세요.</Empty></section> : <>

            <section className="panel proposal-panel" aria-label="다음 액션 추천">
              <label className="mobile-topic-select">분석할 주제<select value={selected} disabled={topicLoading || !topics.length} onChange={event => setSelected(event.target.value)}>{topics.map(topic => <option value={topic.topic_id} key={topic.topic_id}>{topic.name}</option>)}</select></label>
              <div className="proposal-heading"><div><span className="eyebrow">NEXT ACTION · {activeTopic?.name || '주제 선택'}</span><h2>지금 추천하는 다음 액션</h2><p>해야 할 일부터 확인하고, 실행할 작업을 저장하세요.</p></div><button className={proposal ? 'secondary-button' : 'primary-button'} onClick={() => void createProposal()} disabled={!detail || detailLoading || proposing || saving}>{proposing ? '추천 준비 중…' : proposal ? '추천 다시 확인' : '다음 액션 추천받기'}<span aria-hidden="true">↗</span></button></div>
              {(topicLoading || detailLoading) && <Loading>추천에 사용할 분석 근거를 확인하고 있습니다.</Loading>}
              {detailError && <ErrorBox message={detailError} retry={() => setRefresh(value => value + 1)} />}
              {topicError && <ErrorBox message={topicError} retry={() => setRefresh(value => value + 1)} />}
              {proposing && <div className="proposal-loading"><Loading>근거와 기존 액션을 확인하고 제안을 준비합니다.</Loading><p className="muted small">완료되면 검토용 초안이 표시됩니다. 액션은 저장 버튼을 눌러야 등록됩니다.</p></div>}
              {proposalError && <ErrorBox message={proposalError} retry={() => void createProposal()} />}
              {!proposing && !proposal && !proposalError && detail && <div className="recommendation-context">
                <span className="eyebrow">분석에서 먼저 확인할 질문</span><p className="priority-question">{priorityQuestion?.query || activeTopic?.name}</p>
                <p className="context-baseline">{priorityMetric && <>이 질문의 자사 추천 <strong>{ratio(priorityMetric)}개 적격 응답</strong> · </>}주제 수집 커버리지 {percent(detail.evidence.quality.coverage)}</p>
                <p className="muted small">{(detail.evidence.quality.coverage ?? 0) < .8 ? '근거가 부족한 주제입니다. 추천 요청 시 먼저 보완할 수집 범위를 안내합니다.' : activeTopic?.recommendation_rate === 1 ? '현재 적격 응답에서 모두 추천되고 있습니다. 추가 작업이 필요한지 확인합니다.' : '추천을 요청하면 기존 액션과 근거를 함께 확인해, 우선할 작업과 실행 순서를 제안합니다.'}</p>
                <div className="proposal-intro"><span aria-hidden="true">01</span><p>추천 이유</p><span aria-hidden="true">02</span><p>첫 단계와 산출물</p><span aria-hidden="true">03</span><p>검토 후 액션 저장</p></div>
              </div>}
              {proposal && <div className="proposal-result" aria-live="polite"><div className="result-heading"><span className={`tag ${['propose', 'cache_hit'].includes(proposal.result_type) ? 'positive' : 'amber'}`}>{RESULT_LABELS[proposal.result_type]}</span><small className="muted">검토 후 저장 · 데이터 {proposal.dataset_version.slice(0, 10)}</small></div>{!proposal.plan && proposal.reason && <p className="result-reason">{proposal.reason}</p>}{proposal.plan && <p className="review-note"><strong>검토용 초안</strong> · 제품 사실, 자산 선택, 게시 조건을 확인한 뒤 저장하세요.</p>}{proposal.plan ? <PlanView plan={proposal.plan} contracts={proposal.execution_contracts || []} /> : <p className="muted">저장할 액션 초안이 없습니다. 위 안내에 따라 근거와 현재 액션을 확인해 주세요.</p>}
                {proposal.proposal_id && !!proposal.plan?.actions.length && <form className="save-form" onSubmit={event => void saveProposal(event)}><div className="save-form-heading"><div><h3>이 작업을 내 액션으로</h3><p>저장하면 실행 상태와 메모를 관리할 수 있습니다.</p></div>{!savedId && <button className="primary-button" disabled={saving || !saveTitle.trim()}>{saving ? '저장 중…' : '검토한 액션 저장'}</button>}</div><details className="save-edit"><summary>저장할 제목·메모 편집</summary><label>액션 제목<input required maxLength={300} value={saveTitle} disabled={saving || !!savedId} onChange={event => setSaveTitle(event.target.value)} /></label><label>실행 메모 <span className="muted">선택</span><textarea maxLength={4000} rows={3} placeholder="담당자, 사전 확인 사항, 실행 일정 등을 남기세요." value={saveNotes} disabled={saving || !!savedId} onChange={event => setSaveNotes(event.target.value)} /></label></details>{saveError && <ErrorBox message={saveError} />}{savedId ? <div className="saved-confirmation" role="status"><span>액션이 저장되었습니다.</span><button type="button" className="text-button" onClick={() => setPage('actions')}>액션 관리에서 보기 →</button></div> : null}</form>}
              </div>}
            </section>
            <details className="panel evidence-panel" key={selected}><summary className="evidence-toggle"><span>분석 데이터와 원문 확인</span><span className="muted">언급·추천 지표 / 질문 / 인용 출처</span></summary><div className="evidence-content"><div className="detail-heading"><div><span className="eyebrow">분석 근거</span><h2>{activeTopic?.name || selected || '주제 선택'}</h2></div><span className="tag">{selected || '—'}</span></div>{activeTopic?.priority_reason && <p className="priority-reason"><span>우선순위 근거</span>{activeTopic.priority_reason}</p>}{detailLoading || topicLoading ? <Loading>질문과 응답 근거를 불러오고 있습니다.</Loading> : detailError ? <ErrorBox message={detailError} retry={() => setRefresh(value => value + 1)} /> : detail && <EvidenceView key={`${selected}-${refresh}`} evidence={detail.evidence} />}</div></details>
          </>}
        </div>
      </div> : <section className="actions-page"><div className="actions-toolbar"><div><h2>저장한 액션 <span>{actions.length}</span></h2><p className="muted">상태와 메모는 변경할 때마다 서버에 저장됩니다.</p></div><div className="button-row"><select aria-label="액션 상태 필터" value={statusFilter} onChange={event => setStatusFilter(event.target.value)}><option value="all">모든 상태</option>{Object.entries(STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><button className="secondary-button" onClick={() => setActionRefresh(value => value + 1)} disabled={actionsLoading}>새로고침</button></div></div>{actionsLoading ? <Loading>저장한 액션을 불러오고 있습니다.</Loading> : actionsError ? <ErrorBox message={actionsError} retry={() => setActionRefresh(value => value + 1)} /> : filteredActions.length === 0 ? <Empty title={actions.length === 0 ? '아직 저장한 액션이 없습니다.' : '이 상태의 액션이 없습니다.'}>{actions.length === 0 ? <><span>분석 화면에서 제안을 검토한 뒤 첫 액션을 저장해 보세요.</span><br /><button className="text-button" onClick={() => setPage('analysis')}>분석 보러 가기 →</button></> : '다른 상태를 선택해 보세요.'}</Empty> : <div className="actions-list">{filteredActions.map(action => <ActionCard key={action.id} action={action} onUpdate={updateAction} />)}</div>}</section>}
    </main>
    <footer className="app-footer"><span>VIVI · Evidence to action</span><span>제공된 합성 데이터 기반 · 관측 변화가 인과 효과를 의미하지 않습니다.</span></footer>
  </div>;
}
