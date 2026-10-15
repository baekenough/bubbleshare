from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import shutil

from latticedb import Database
import pytest

from vivi_agent.evidence import EvidenceRepository
from vivi_agent.relations import RelationProjection

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "VIVI_MVP_Sample_Dataset"


def test_actual_paths_match_original_citations_and_ownership_on_reopen(tmp_path):
    repo = EvidenceRepository(DATASET)
    pack = repo.build("lead")
    projection = RelationProjection(repo, tmp_path)
    actual = projection.paths(pack)
    current = {row["observation_id"]: row for row in pack["observations"]}
    sources = {row["source_id"]: row for row in json.loads((DATASET / "sources.json").read_text())}
    expected = {
        (current[row["observation_id"]]["question_id"], row["observation_id"], row["source_id"],
         sources[row["source_id"]]["brand_id"] if sources[row["source_id"]]["is_brand_owned"] else None)
        for row in json.loads((DATASET / "citations.json").read_text())
        if row["observation_id"] in current
    }
    assert actual["backend"] == "latticedb"
    assert actual["dataset_version"] == repo.dataset_hash
    assert expected
    assert {tuple(path[key] for key in ("question_id", "observation_id", "source_id", "brand_id"))
            for path in actual["paths"]} == expected
    assert any(path["brand_id"] is None for path in actual["paths"])
    assert any(path["brand_id"] is not None for path in actual["paths"])
    assert RelationProjection(repo, tmp_path).paths(pack) == actual
    with Database(str(projection.path), read_only=True) as db:
        assert list(db.query("MATCH (p:Projection) RETURN count(p) AS total")) == [{"total": 1}]
        assert list(db.query("MATCH (r:Response)-[:MENTIONS]->(b:Brand) RETURN count(r) AS total"))[0]["total"] > 0


def test_previous_excluded_and_out_of_scope_responses_never_leak(tmp_path):
    repo = EvidenceRepository(DATASET)
    projection = RelationProjection(repo, tmp_path)
    for topic in ("lead", "scale", "automation"):
        pack = repo.build(topic)
        result = projection.paths(pack)
        returned = {path["observation_id"] for path in result["paths"]}
        assert returned <= {row["observation_id"] for row in pack["observations"]}
        assert returned.isdisjoint(row["observation_id"] for row in pack["excluded"])
        assert returned.isdisjoint(row["observation_id"] for row in pack["previous_comparison"]["previous_observations"])
        empty = deepcopy(pack)
        empty["observations"] = []
        assert projection.paths(empty)["paths"] == []


def test_rejects_pack_and_persisted_version_mismatch(tmp_path):
    repo = EvidenceRepository(DATASET)
    pack = repo.build("lead")
    projection = RelationProjection(repo, tmp_path)
    with pytest.raises(ValueError, match="version mismatch"):
        projection.paths({**pack, "dataset_hash": "different"})
    assert not projection.path.exists()
    projection.paths(pack)
    with Database(str(projection.path)) as db:
        with db.write() as tx:
            tx.create_node(labels=["Projection"], properties={"dataset_version": "different"})
            tx.commit()
    with pytest.raises(ValueError, match="version mismatch"):
        projection.paths(pack)


def test_build_rejects_raw_files_changed_since_repository_loaded(tmp_path):
    copied = tmp_path / "dataset"
    shutil.copytree(DATASET, copied)
    repo = EvidenceRepository(copied)
    source_path = copied / "sources.json"
    source_path.write_text(source_path.read_text() + "\n")
    with pytest.raises(ValueError, match="version mismatch"):
        RelationProjection(repo, tmp_path / "state").paths(repo.build("lead"))


def test_concurrent_open_and_query_is_serialized(tmp_path):
    repo = EvidenceRepository(DATASET)
    pack = repo.build("lead")
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda _: RelationProjection(repo, tmp_path).paths(pack), range(3)))
    assert results[0]["paths"]
    assert results[0] == results[1] == results[2]
