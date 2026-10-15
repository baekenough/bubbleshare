"""제공된 데이터셋에서 읽기 전용 근거팩을 계산한다.

AI 응답은 관측 자료이며 제품 기능의 확인 근거가 아니다.
출처 레코드는 메타데이터이고, 이 저장소는 페이지 본문에 접근하지 않는다.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any


DEFAULT_BUSINESS_CONTEXT = {
    "verified_product_facts": [],
    "verified_product_facts_status": "unknown",
}


def _json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_business_context(context: dict | None) -> dict:
    """영수증이 있는 기능 확인 결과를 원래 검증 범위 안에서 사실로 제공한다."""
    result = {**deepcopy(DEFAULT_BUSINESS_CONTEXT), **deepcopy(context or {})}
    supported = result.get("verified_web_form_auto_registration")
    receipt = result.get("evidence_receipt")
    if not isinstance(supported, bool) or not isinstance(receipt, str) or not receipt.strip():
        return result
    if supported:
        statement = "웹 문의의 CRM 리드 자동 등록 지원이 확인되었습니다."
        route = result.get("verified_route")
        if isinstance(route, str) and route.strip():
            statement += f" 검증된 경로: {route}. 이 경로만으로 네이티브 기능 지원을 추론하지 않습니다."
        else:
            statement += " 구현 경로와 네이티브 기능 지원 여부는 확인되지 않았습니다."
    else:
        statement = "웹 문의의 CRM 리드 자동 등록은 지원되지 않는 것으로 확인되었습니다."
    fact = {"statement": statement, "receipt_id": receipt, "origin": deepcopy(result.get("origin"))}
    facts = result["verified_product_facts"]
    if not isinstance(facts, list):
        raise TypeError("verified_product_facts must be a list when normalizing a receipted capability check")
    if fact not in facts:
        facts.append(fact)
    result["verified_product_facts_status"] = "partially_verified"
    return result


class EvidenceRepository:
    """입력 스냅샷을 보존하며 build/lookup마다 독립된 딕셔너리를 반환한다."""

    _TABLES = ("brands", "questions", "observations", "mentions", "citations", "sources")

    def __init__(self, dataset_dir: Path):
        self.dataset_dir = Path(dataset_dir)
        raw = {
            name: (self.dataset_dir / f"{name}.json").read_bytes()
            for name in (*self._TABLES, "manifest")
        }
        self._data = {name: json.loads(contents) for name, contents in raw.items()}
        digest = hashlib.sha256()
        for name in sorted(raw):
            digest.update(name.encode("utf-8") + b"\0")
            digest.update(str(len(raw[name])).encode("ascii") + b"\0")
            digest.update(raw[name])
        self.dataset_hash = digest.hexdigest()
        self._target = self._data["manifest"]["target_brand_id"]
        self._questions = {row["question_id"]: row for row in self._data["questions"]}
        self._sources = {row["source_id"]: row for row in self._data["sources"]}
        self._mentions: dict[str, list[dict]] = defaultdict(list)
        self._citations: dict[str, list[dict]] = defaultdict(list)
        for row in self._data["mentions"]:
            self._mentions[row["observation_id"]].append(row)
        for row in self._data["citations"]:
            self._citations[row["observation_id"]].append(row)
        self._channels = sorted({row["channel"] for row in self._data["observations"]})
        self._groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for row in self._data["observations"]:
            self._groups[(row["question_id"], row["channel"], self._batch(row))].append(row)
        self._canonical = {
            key: max(rows, key=self._selection_order) for key, rows in self._groups.items()
        }
        self._batch_dates: dict[str, datetime] = {}
        for row in self._data["observations"]:
            batch, collected_at = self._batch(row), datetime.fromisoformat(row["collected_at"])
            self._batch_dates[batch] = min(self._batch_dates.get(batch, collected_at), collected_at)

    @staticmethod
    def _batch(row: dict) -> str:
        # 날짜로 대체한 스냅샷은 이름 있는 스냅샷과 구분한다.
        return row.get("source_snapshot_id") or (
            "date:" + datetime.fromisoformat(row["collected_at"]).date().isoformat()
        )

    def _selection_order(self, row: dict) -> tuple:
        return (
            {"complete": 2, "partial": 1, "failed": 0}[row["collection_status"]],
            row["response_language"] == self._questions[row["question_id"]]["language"],
            datetime.fromisoformat(row["collected_at"]),
            row["observation_id"],
        )

    def _exclusion_reason(self, row: dict) -> str | None:
        if row["collection_status"] != "complete":
            return row["collection_status"]
        if row["response_language"] != self._questions[row["question_id"]]["language"]:
            return "language_mismatch"
        return None

    def _mentioned(self, row: dict, brand_id: str | None = None) -> bool:
        return any(
            mention["brand_id"] == (brand_id or self._target)
            for mention in self._mentions[row["observation_id"]]
        )

    def _recommended(self, row: dict, brand_id: str | None = None) -> bool:
        return any(
            mention["brand_id"] == (brand_id or self._target)
            and mention["mention_context"] == "recommended"
            for mention in self._mentions[row["observation_id"]]
        )

    def _owned_cited(self, row: dict, brand_id: str | None = None) -> bool:
        return any(
            self._sources[citation["source_id"]]["is_brand_owned"]
            and self._sources[citation["source_id"]]["brand_id"] == (brand_id or self._target)
            for citation in self._citations[row["observation_id"]]
        )

    def _enrich(self, row: dict) -> dict:
        result = deepcopy(row)
        result["mentions"] = deepcopy(self._mentions[row["observation_id"]])
        result["citations"] = [
            {**deepcopy(citation), "source": deepcopy(self._sources[citation["source_id"]])}
            for citation in self._citations[row["observation_id"]]
        ]
        # 제공된 요약은 보존하되 이후 계산에는 여기서 다시 구한 값을 쓴다.
        result["recomputed"] = {
            "target_mentioned": self._mentioned(row),
            "mention_count": len(result["mentions"]),
            "citation_count": len(result["citations"]),
        }
        return result

    @staticmethod
    def _metric(rows: list[dict], predicate, description: str) -> dict:
        supporting = [row["observation_id"] for row in rows if predicate(row)]
        return {
            "numerator": len(supporting),
            "denominator": len(rows),
            "description": description,
            "observation_ids": [row["observation_id"] for row in rows],
            "supporting_observation_ids": supporting,
        }

    def build(self, topic_id: str, snapshot_id: str = "SNAP-2", context: dict | None = None) -> dict:
        if context is not None and not isinstance(context, dict):
            raise TypeError("context must be a dict or None")
        questions = sorted(
            (row for row in self._questions.values() if row["topic_id"] == topic_id),
            key=lambda row: row["question_id"],
        )
        if not questions:
            raise ValueError(f"Unknown topic_id: {topic_id}")
        if snapshot_id not in self._batch_dates:
            raise ValueError(f"Unknown snapshot_id: {snapshot_id}")
        question_ids = {row["question_id"] for row in questions}
        current = sorted(
            (row for (question, _, batch), row in self._canonical.items()
             if question in question_ids and batch == snapshot_id),
            key=lambda row: (row["question_id"], row["channel"]),
        )
        eligible = [row for row in current if self._exclusion_reason(row) is None]
        excluded = []
        for key, attempts in sorted(self._groups.items()):
            if key[0] not in question_ids or key[2] != snapshot_id:
                continue
            canonical = self._canonical[key]
            for row in attempts:
                reason = "superseded" if row is not canonical else self._exclusion_reason(row)
                if reason:
                    excluded.append({
                        **self._enrich(row),
                        "reason": reason,
                        "canonical_observation_id": canonical["observation_id"],
                    })

        metrics = {}

        def add_target_metrics(prefix: str, rows: list[dict]):
            metrics[prefix + ".target_mentions"] = self._metric(
                rows, self._mentioned, "Responses with a target-brand mention, divided by eligible responses."
            )
            metrics[prefix + ".target_recommendations"] = self._metric(
                rows, self._recommended, "Responses recommending the target brand, divided by eligible responses."
            )
            metrics[prefix + ".target_owned_citations"] = self._metric(
                rows, self._owned_cited,
                "Responses citing a source explicitly owned by the target brand, divided by eligible responses; unresolved ownership is excluded."
            )

        add_target_metrics("topic", eligible)
        metrics["topic.target_cited_without_mention"] = self._metric(
            eligible, lambda row: self._owned_cited(row) and not self._mentioned(row),
            "Responses citing a target-owned source without mentioning the target brand."
        )
        for channel in self._channels:
            add_target_metrics("channel." + channel, [row for row in eligible if row["channel"] == channel])
        for question_id in sorted(question_ids):
            add_target_metrics("question." + question_id, [row for row in eligible if row["question_id"] == question_id])
        for brand in self._data["brands"]:
            brand_id = brand["brand_id"]
            metrics[f"brand.{brand_id}.mentions"] = self._metric(
                eligible, lambda row, bid=brand_id: self._mentioned(row, bid),
                f"Eligible responses mentioning {brand['name']}."
            )
            metrics[f"brand.{brand_id}.recommendations"] = self._metric(
                eligible, lambda row, bid=brand_id: self._recommended(row, bid),
                f"Eligible responses recommending {brand['name']}."
            )
            metrics[f"brand.{brand_id}.owned_citations"] = self._metric(
                eligible, lambda row, bid=brand_id: self._owned_cited(row, bid),
                f"Eligible responses citing at least one source explicitly owned by {brand['name']}; count each response once, not each citation, and exclude unresolved ownership."
            )

        earlier = [
            batch for batch, timestamp in self._batch_dates.items()
            if timestamp < self._batch_dates[snapshot_id]
        ]
        previous_snapshot = max(earlier, key=self._batch_dates.get) if earlier else None
        pairs, previous_rows, matched_current = [], [], []
        for row in eligible:
            previous = self._canonical.get((row["question_id"], row["channel"], previous_snapshot))
            if previous is None or self._exclusion_reason(previous) is not None:
                continue
            previous_rows.append(previous)
            matched_current.append(row)
            pairs.append({
                "question_id": row["question_id"],
                "channel": row["channel"],
                "previous_observation_id": previous["observation_id"],
                "current_observation_id": row["observation_id"],
                "previous_target_mentioned": self._mentioned(previous),
                "current_target_mentioned": self._mentioned(row),
            })
        add_target_metrics("comparison.previous", previous_rows)
        add_target_metrics("comparison.current", matched_current)
        previous_comparison = {
            "previous_snapshot_id": previous_snapshot,
            "current_snapshot_id": snapshot_id,
            "matched_count": len(pairs),
            "pairs": pairs,
            "previous_observations": [self._enrich(row) for row in previous_rows],
            "previous_target_mentions": deepcopy(metrics["comparison.previous.target_mentions"]),
            "current_target_mentions": deepcopy(metrics["comparison.current.target_mentions"]),
            "interpretation": "Same question/channel pairs eligible at both snapshots; descriptive association, not a causal effect.",
        }

        source_ids = {
            citation["source_id"]
            for row in [*eligible, *previous_rows, *excluded]
            for citation in self._citations[row["observation_id"]]
        }
        source_ids.update(source_id for source_id, source in self._sources.items() if source["is_brand_owned"])
        source_rows = [deepcopy(self._sources[source_id]) for source_id in sorted(source_ids)]
        for source_id in sorted(source_ids):
            metrics[f"source.{source_id}.citation_responses"] = self._metric(
                eligible,
                lambda row, sid=source_id: any(c["source_id"] == sid for c in self._citations[row["observation_id"]]),
                "Eligible current responses citing this source; metadata does not establish what its body says."
            )
        business_context = _normalize_business_context(context)
        expected = len(questions) * len(self._channels)
        pack = {
            "scope": {"topic_id": topic_id, "snapshot_id": snapshot_id, "target_brand_id": self._target},
            "dataset_hash": self.dataset_hash,
            "business_context": business_context,
            "context_hash": _json_hash(business_context),
            "questions": deepcopy(questions),
            "metrics": metrics,
            "observations": [self._enrich(row) for row in eligible],
            "excluded": excluded,
            "sources": source_rows,
            "previous_comparison": previous_comparison,
            "quality": {
                "eligible": len(eligible),
                "expected": expected,
                "coverage": len(eligible) / expected if expected else None,
                "canonical_observations": len(current),
                "superseded_attempts": sum(row["reason"] == "superseded" for row in excluded),
                "by_channel": {
                    channel: {
                        "eligible": sum(row["channel"] == channel for row in eligible),
                        "expected": len(questions),
                        "coverage": sum(row["channel"] == channel for row in eligible) / len(questions),
                    }
                    for channel in self._channels
                },
            },
            "capabilities": {"body_access": False},
        }
        pack["allowed_evidence_ids"] = sorted(self._reference_index(pack))
        return pack

    @staticmethod
    def _reference_index(pack: dict) -> dict[str, dict]:
        index = {}
        for row in pack["questions"]:
            index[row["question_id"]] = {"evidence_id": row["question_id"], "kind": "question", **row}
        for row in pack["sources"]:
            index[row["source_id"]] = {"evidence_id": row["source_id"], "kind": "source_metadata", **row}
        rows = [*pack["observations"], *pack["excluded"], *pack["previous_comparison"]["previous_observations"]]
        for row in rows:
            index[row["observation_id"]] = {"evidence_id": row["observation_id"], "kind": "observation", **row}
            for mention in row["mentions"]:
                index[mention["mention_id"]] = {"evidence_id": mention["mention_id"], "kind": "mention", **mention}
            for citation in row["citations"]:
                index[citation["citation_id"]] = {"evidence_id": citation["citation_id"], "kind": "citation", **citation}
        for metric_id, metric in pack["metrics"].items():
            index[metric_id] = {"evidence_id": metric_id, "kind": "metric", **metric}
        # 확인된 고객 사실도 출처를 유지하며 근거에 포함한다.
        receipts = defaultdict(list)
        for fact in pack["business_context"].get("verified_product_facts", []):
            if isinstance(fact, dict) and isinstance(fact.get("receipt_id"), str) and fact.get("receipt_id") and fact.get("statement"):
                receipts[fact["receipt_id"]].append(deepcopy(fact))
        for receipt_id, facts in receipts.items():
            if receipt_id in index:
                raise ValueError("Product verification receipt conflicts with a dataset evidence ID")
            index[receipt_id] = {"evidence_id": receipt_id, "kind": "product_verification", "facts": facts}
        return index

    def lookup(self, pack: dict, ids: list[str]) -> list[dict]:
        """이 근거팩에 포함된 ID만 조회하며 알 수 없거나 범위를 벗어난 ID는 거부한다."""
        if pack.get("dataset_hash") != self.dataset_hash:
            raise ValueError("Evidence pack belongs to a different dataset snapshot")
        index = self._reference_index(pack)
        allowed = set(pack["allowed_evidence_ids"])
        unknown = [value for value in ids if value not in index or value not in allowed]
        if unknown:
            raise ValueError("Unknown or out-of-scope evidence IDs: " + ", ".join(unknown))
        return [deepcopy(index[value]) for value in ids]
