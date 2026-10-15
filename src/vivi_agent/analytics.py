"""원본에서 다시 만들 수 있는 DuckDB 조회 모델. 관측 적격성 규칙은 공통으로 쓴다."""
from __future__ import annotations

import fcntl
from pathlib import Path

import duckdb

from .measurement import _selector


class DuckDBAnalytics:
    def __init__(self, repository, state_dir):
        self.repository = repository
        self.dataset_version = repository.dataset_hash
        root = Path(state_dir) / 'analytics'
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / f'{self.dataset_version}.duckdb'
        with (root / 'build.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not self.path.exists():
                from .evidence import EvidenceRepository
                if EvidenceRepository(repository.dataset_dir).dataset_hash != self.dataset_version:
                    raise ValueError('Raw dataset changed before analytics projection build')
                temporary = self.path.with_suffix('.building.duckdb')
                temporary.unlink(missing_ok=True)
                with duckdb.connect(str(temporary)) as db:
                    for table in ('questions', 'observations', 'mentions', 'citations', 'sources', 'brands'):
                        db.execute(f'CREATE TABLE {table} AS SELECT * FROM read_json_auto(?)',
                                   [str(repository.dataset_dir / f'{table}.json')])
                    db.execute('CREATE TABLE projection_version AS SELECT ? AS version', [self.dataset_version])
                    db.execute('CHECKPOINT')
                temporary.replace(self.path)

    def build(self, topic_id, snapshot_id='SNAP-2', context=None):
        pack = self.repository.build(topic_id, snapshot_id, context)
        return self.aggregate(pack)

    def aggregate(self, pack):
        if pack['dataset_hash'] != self.dataset_version:
            raise ValueError('Analytics/evidence dataset version mismatch')
        with duckdb.connect(str(self.path), read_only=True) as db:
            if db.execute('SELECT version FROM projection_version').fetchone()[0] != self.dataset_version:
                raise ValueError('Stored analytics projection version mismatch')
            db.execute('CREATE TEMP TABLE selected_metrics(metric_id VARCHAR, kind VARCHAR, brand_id VARCHAR, source_id VARCHAR)')
            db.execute('CREATE TEMP TABLE members(metric_id VARCHAR, observation_id VARCHAR)')
            definitions, members = [], []
            for mid, metric in pack['metrics'].items():
                selector = _selector(mid, pack['scope']['target_brand_id'])
                definitions.append((mid, selector['kind'], selector.get('brand_id'),
                                    (selector.get('source_ids') or [None])[0]))
                members.extend((mid, oid) for oid in metric['observation_ids'])
            db.executemany('INSERT INTO selected_metrics VALUES (?,?,?,?)', definitions)
            if members:
                db.executemany('INSERT INTO members VALUES (?,?)', members)
            rows = db.execute('''
                WITH flags AS (
                  SELECT d.metric_id, m.observation_id,
                    EXISTS(SELECT 1 FROM mentions n WHERE n.observation_id=m.observation_id AND n.brand_id=d.brand_id) AS mentioned,
                    EXISTS(SELECT 1 FROM mentions n WHERE n.observation_id=m.observation_id AND n.brand_id=d.brand_id AND n.mention_context='recommended') AS recommended,
                    EXISTS(SELECT 1 FROM citations c JOIN sources s USING(source_id) WHERE c.observation_id=m.observation_id AND s.is_brand_owned=true AND s.brand_id=d.brand_id) AS owned,
                    EXISTS(SELECT 1 FROM citations c WHERE c.observation_id=m.observation_id AND c.source_id=d.source_id) AS source_cited,
                    d.kind
                  FROM selected_metrics d LEFT JOIN members m USING(metric_id)
                ), hits AS (
                  SELECT *, CASE kind WHEN 'mentions' THEN mentioned WHEN 'recommendations' THEN recommended
                    WHEN 'owned_citations' THEN owned WHEN 'cited_without_mention' THEN owned AND NOT mentioned
                    WHEN 'source_citation' THEN source_cited ELSE false END AS hit FROM flags
                )
                SELECT metric_id, count(observation_id), count(observation_id) FILTER(WHERE hit),
                  coalesce(list(observation_id ORDER BY observation_id) FILTER(WHERE observation_id IS NOT NULL), []),
                  coalesce(list(observation_id ORDER BY observation_id) FILTER(WHERE hit AND observation_id IS NOT NULL), [])
                FROM hits GROUP BY metric_id
            ''').fetchall()
        for mid, denominator, numerator, ids, supporting in rows:
            metric = pack['metrics'][mid]
            # 전환 중에는 기존 계산 결과와 독립적으로 대조한다.
            if (denominator != metric['denominator'] or numerator != metric['numerator']
                    or set(supporting) != set(metric['supporting_observation_ids'])):
                raise ValueError(f'DuckDB aggregation disagrees with canonical evidence: {mid}')
            metric.update(numerator=numerator, denominator=denominator,
                          observation_ids=ids, supporting_observation_ids=supporting)
        pack['dataset_version'] = self.dataset_version
        pack['analytics_backend'] = 'duckdb'
        return pack


def priorities(pack):
    """관측 공백 × 중요도 × 수집률로 순위를 매긴다. 예상 개선 효과를 뜻하지 않는다."""
    rows = []
    expected = len(pack['quality']['by_channel'])
    for question in pack['questions']:
        mid = f"question.{question['question_id']}.target_recommendations"
        metric = pack['metrics'][mid]
        denominator = metric['denominator']
        coverage = denominator / expected if expected else 0
        gap = 1 - metric['numerator'] / denominator if denominator else 0
        score = round(question['importance_score'] * gap * coverage, 2)
        rows.append({'question_id': question['question_id'], 'score': score,
                     'metric_id': mid, 'importance': question['importance_score'],
                     'coverage': coverage, 'recommendation_gap': gap,
                     'reason': f"{question['question_id']} 기준: 중요도 {question['importance_score']} × 추천 공백 {gap:.2f} × 유효 수집률 {coverage:.2f} = {score}"})
    return sorted(rows, key=lambda row: (-row['score'], row['question_id']))
