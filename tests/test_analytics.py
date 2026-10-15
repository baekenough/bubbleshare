from pathlib import Path

import pytest

from vivi_agent.analytics import DuckDBAnalytics, priorities
from vivi_agent.evidence import EvidenceRepository

ROOT = Path(__file__).resolve().parents[1]


def test_duckdb_counts_match_all_canonical_topics_and_snapshots(tmp_path):
    repo = EvidenceRepository(ROOT / 'VIVI_MVP_Sample_Dataset')
    analytics = DuckDBAnalytics(repo, tmp_path)
    for topic in {q['topic_id'] for q in repo._questions.values()}:
        for snapshot in ('SNAP-1', 'SNAP-2'):
            pack = analytics.build(topic, snapshot)
            reference = repo.build(topic, snapshot)
            assert pack['analytics_backend'] == 'duckdb'
            for mid, metric in pack['metrics'].items():
                assert (metric['numerator'], metric['denominator']) == (reference['metrics'][mid]['numerator'], reference['metrics'][mid]['denominator'])
    lead = analytics.build('lead')
    assert lead['metrics']['question.Q068.target_mentions']['denominator'] == 3
    assert priorities(lead)[0]['question_id'] == 'Q068'
    assert analytics.path.exists()
    assert DuckDBAnalytics(repo, tmp_path).path == analytics.path


def test_dataset_versions_cannot_be_mixed(tmp_path):
    repo = EvidenceRepository(ROOT / 'VIVI_MVP_Sample_Dataset')
    pack = repo.build('lead'); pack['dataset_hash'] = 'other'
    with pytest.raises(ValueError, match='version mismatch'):
        DuckDBAnalytics(repo, tmp_path).aggregate(pack)
