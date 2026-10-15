"""원본 데이터셋에서 다시 만들 수 있는 LatticeDB 관계 조회 모델."""

from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path

from latticedb import Database


class RelationProjection:
    """그래프 내부 ID 대신 근거팩의 원본 ID만 반환한다.

    LatticeDB는 한 프로세스만 파일을 소유하므로 열기·조회·닫기 전체에 파일 잠금을 건다.
    이 조회 모델에는 변경 가능한 사용자 상태나 업무 상태를 저장하지 않는다.
    """

    def __init__(self, repository, state_dir: Path):
        self.repository = repository
        self.dataset_version = repository.dataset_hash
        directory = Path(state_dir) / "relations"
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{self.dataset_version}.lattice"
        self.lock_path = directory / f"{self.dataset_version}.lock"

    def _raw_tables(self) -> dict:
        names = ("brands", "questions", "observations", "mentions", "citations", "sources", "manifest")
        raw = {name: (self.repository.dataset_dir / f"{name}.json").read_bytes() for name in names}
        digest = hashlib.sha256()
        for name in sorted(raw):
            digest.update(name.encode("utf-8") + b"\0")
            digest.update(str(len(raw[name])).encode("ascii") + b"\0")
            digest.update(raw[name])
        if digest.hexdigest() != self.dataset_version:
            raise ValueError("Dataset version mismatch while building relation projection")
        return {name: json.loads(contents) for name, contents in raw.items()}

    def _build(self, db) -> None:
        tables = self._raw_tables()
        with db.write() as tx:
            nodes = {}
            for table, label, key in (
                ("brands", "Brand", "brand_id"),
                ("questions", "Question", "question_id"),
                ("observations", "Response", "observation_id"),
                ("sources", "Source", "source_id"),
            ):
                for row in tables[table]:
                    nodes[(label, row[key])] = tx.create_node(
                        labels=[label], properties={"external_id": row[key]}
                    ).id
            edges = set()

            def edge(left_label, left_id, relation, right_label, right_id):
                signature = (left_label, left_id, relation, right_label, right_id)
                if signature not in edges:
                    tx.create_edge(nodes[(left_label, left_id)], nodes[(right_label, right_id)], relation)
                    edges.add(signature)

            for row in tables["observations"]:
                edge("Question", row["question_id"], "ANSWERED_BY", "Response", row["observation_id"])
            for row in tables["citations"]:
                edge("Response", row["observation_id"], "CITES", "Source", row["source_id"])
            for row in tables["sources"]:
                if row["is_brand_owned"] and row.get("brand_id") is not None:
                    edge("Source", row["source_id"], "OWNED_BY", "Brand", row["brand_id"])
            for row in tables["mentions"]:
                edge("Response", row["observation_id"], "MENTIONS", "Brand", row["brand_id"])
            tx.create_node(labels=["Projection"], properties={"dataset_version": self.dataset_version})
            tx.commit()

    def paths(self, pack: dict) -> dict:
        if pack.get("dataset_hash") != self.dataset_version:
            raise ValueError("Dataset version mismatch in relation projection")
        current_ids = {row["observation_id"] for row in pack["observations"]}
        question_ids = {row["question_id"] for row in pack["questions"]}
        source_ids = {row["source_id"] for row in pack["sources"]}
        with self.lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            with Database(str(self.path), create=True, enable_vectors=False) as db:
                metadata = list(db.query("MATCH (p:Projection) RETURN p.dataset_version AS dataset_version"))
                if not metadata:
                    count = list(db.query("MATCH (n) RETURN count(n) AS total"))[0]["total"]
                    if count:
                        raise ValueError("Dataset version mismatch: relation projection metadata missing")
                    self._build(db)
                elif metadata != [{"dataset_version": self.dataset_version}]:
                    raise ValueError("Dataset version mismatch in persisted relation projection")
                owners = {
                    row["source_id"]: row["brand_id"]
                    for row in db.query(
                        "MATCH (s:Source)-[:OWNED_BY]->(b:Brand) "
                        "RETURN s.external_id AS source_id, b.external_id AS brand_id"
                    )
                }
                paths = {
                    (row["question_id"], row["observation_id"], row["source_id"])
                    for row in db.query(
                        "MATCH (q:Question)-[:ANSWERED_BY]->(r:Response)-[:CITES]->(s:Source) "
                        "RETURN q.external_id AS question_id, r.external_id AS observation_id, "
                        "s.external_id AS source_id"
                    )
                    if row["observation_id"] in current_ids
                    and row["question_id"] in question_ids
                    and row["source_id"] in source_ids
                }
        return {
            "dataset_version": self.dataset_version,
            "backend": "latticedb",
            "paths": [
                {"question_id": question, "observation_id": observation,
                 "source_id": source, "brand_id": owners.get(source)}
                for question, observation, source in sorted(paths)
            ],
        }
