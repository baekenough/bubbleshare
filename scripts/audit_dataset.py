#!/usr/bin/env python3
"""표준 라이브러리로 제공된 VIVI 데이터를 읽기 전용 감사한다.

어느 디렉터리에서나 실행 가능:
    python3 /path/to/bubbleshare/scripts/audit_dataset.py --output docs/data-audit.json

상대 경로는 저장소 루트를 기준으로 해석한다.
원본 JSON을 보존하고 정규화 판단을 각각 기록한다.
"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PRIMARY_KEYS = {
    "brands": "brand_id",
    "questions": "question_id",
    "observations": "observation_id",
    "mentions": "mention_id",
    "citations": "citation_id",
    "sources": "source_id",
}
FOREIGN_KEYS = [
    ("observations", "question_id", "questions", "question_id"),
    ("mentions", "observation_id", "observations", "observation_id"),
    ("mentions", "brand_id", "brands", "brand_id"),
    ("citations", "observation_id", "observations", "observation_id"),
    ("citations", "source_id", "sources", "source_id"),
    ("sources", "brand_id", "brands", "brand_id"),
]


def ratio(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": round(numerator / denominator, 6) if denominator else None,
    }


def batch_id(observation):
    snapshot = observation.get("source_snapshot_id")
    if snapshot:
        return "snapshot:" + snapshot
    return "date:" + datetime.fromisoformat(observation["collected_at"]).date().isoformat()


def audit(dataset_dir):
    tables = {
        name: json.loads((dataset_dir / (name + ".json")).read_text(encoding="utf-8"))
        for name in PRIMARY_KEYS
    }
    manifest = json.loads((dataset_dir / "manifest.json").read_text(encoding="utf-8"))
    target = manifest["target_brand_id"]
    questions = {row["question_id"]: row for row in tables["questions"]}
    brands = {row["brand_id"]: row for row in tables["brands"]}
    sources = {row["source_id"]: row for row in tables["sources"]}
    observations = tables["observations"]
    mentions = defaultdict(list)
    citations = defaultdict(list)
    for row in tables["mentions"]:
        mentions[row["observation_id"]].append(row)
    for row in tables["citations"]:
        citations[row["observation_id"]].append(row)

    def target_mentioned(observation):
        return any(row["brand_id"] == target for row in mentions[observation["observation_id"]])

    def eligible(observation):
        return (
            observation["collection_status"] == "complete"
            and observation["response_language"] == questions[observation["question_id"]]["language"]
        )

    def selection_order(observation):
        return (
            {"complete": 2, "partial": 1, "failed": 0}[observation["collection_status"]],
            observation["response_language"] == questions[observation["question_id"]]["language"],
            datetime.fromisoformat(observation["collected_at"]),
            observation["observation_id"],
        )

    groups = defaultdict(list)
    for row in observations:
        groups[(row["question_id"], row["channel"], batch_id(row))].append(row)
    canonical = sorted(
        (max(rows, key=selection_order) for rows in groups.values()),
        key=lambda row: row["observation_id"],
    )
    valid = [row for row in canonical if eligible(row)]

    def metrics(rows):
        return {
            "observation_count": len(rows),
            "target_mentions_from_children": ratio(sum(target_mentioned(row) for row in rows), len(rows)),
            "target_mentions_from_summary": ratio(sum(row["target_mentioned"] for row in rows), len(rows)),
            "mention_rows": sum(len(mentions[row["observation_id"]]) for row in rows),
            "citation_rows": sum(len(citations[row["observation_id"]]) for row in rows),
        }

    mismatches = {}
    recompute = {
        "target_mentioned": target_mentioned,
        "mention_count": lambda row: len(mentions[row["observation_id"]]),
        "citation_count": lambda row: len(citations[row["observation_id"]]),
    }
    for field, calculate in recompute.items():
        items = [
            {
                "observation_id": row["observation_id"],
                "reported": row[field],
                "recomputed": calculate(row),
                "quality_flag": row["quality_flag"],
            }
            for row in observations
            if row[field] != calculate(row)
        ]
        mismatches[field] = {"count": len(items), "rows": items}

    def child_signature(observation, table, ignored):
        return sorted(
            json.dumps({key: value for key, value in row.items() if key not in ignored}, sort_keys=True)
            for row in table[observation["observation_id"]]
        )

    duplicates = []
    for key, rows in sorted(groups.items()):
        if len(rows) < 2:
            continue
        chosen = max(rows, key=selection_order)
        first = rows[0]
        duplicates.append({
            "key": {"question_id": key[0], "channel": key[1], "batch_id": key[2]},
            "canonical_observation_id": chosen["observation_id"],
            "discarded_observation_ids": sorted(row["observation_id"] for row in rows if row is not chosen),
            "attempts": [
                {field: row[field] for field in ("observation_id", "collected_at", "quality_flag", "collection_status")}
                for row in rows
            ],
            "response_text_identical": all(row["response_text"] == first["response_text"] for row in rows),
            "mentions_identical_ignoring_ids": all(
                child_signature(row, mentions, {"mention_id", "observation_id"})
                == child_signature(first, mentions, {"mention_id", "observation_id"})
                for row in rows
            ),
            "citations_identical_ignoring_ids": all(
                child_signature(row, citations, {"citation_id", "observation_id"})
                == child_signature(first, citations, {"citation_id", "observation_id"})
                for row in rows
            ),
        })

    pk_issues = {}
    for name, key in PRIMARY_KEYS.items():
        counts = Counter(row[key] for row in tables[name])
        pk_issues[name] = sorted(value for value, count in counts.items() if count > 1)
    fk_issues = {}
    for child, key, parent, parent_key in FOREIGN_KEYS:
        allowed = {row[parent_key] for row in tables[parent]}
        fk_issues[child + "." + key] = [
            row[PRIMARY_KEYS[child]] for row in tables[child]
            if row[key] is not None and row[key] not in allowed
        ]

    # 하위 도메인 일치는 소유 후보 근거로만 쓰고 원본 메타데이터는 보존한다.
    ownership_candidates = {}
    for source in sources.values():
        if source["is_brand_owned"] and not source["brand_id"]:
            matches = [
                brand_id for brand_id, brand in brands.items()
                if source["domain"].lower() == brand["domain"].lower()
                or source["domain"].lower().endswith("." + brand["domain"].lower())
            ]
            if len(matches) == 1:
                ownership_candidates[source["source_id"]] = matches[0]

    def owned_citation_count(rows, include_candidates=False):
        return sum(any(
            sources[citation["source_id"]]["is_brand_owned"] and (
                sources[citation["source_id"]]["brand_id"] == target
                or (include_candidates and ownership_candidates.get(citation["source_id"]) == target)
            )
            for citation in citations[row["observation_id"]]
        ) for row in rows)

    channels = sorted({row["channel"] for row in observations})
    batches = sorted({batch_id(row) for row in observations}, key=lambda value: min(
        datetime.fromisoformat(row["collected_at"]) for row in observations if batch_id(row) == value
    ))
    latest_batch = batches[-1]
    expected_per_batch = len(questions) * len(channels)
    snapshot_metrics = []
    for batch in batches:
        raw_rows = [row for row in observations if batch_id(row) == batch]
        canonical_rows = [row for row in canonical if batch_id(row) == batch]
        valid_rows = [row for row in valid if batch_id(row) == batch]
        snapshot_metrics.append({
            "batch_id": batch,
            "collection_date": min(row["collected_at"][:10] for row in raw_rows),
            "expected_observations": expected_per_batch,
            "raw": metrics(raw_rows),
            "canonical": metrics(canonical_rows),
            "eligible": metrics(valid_rows),
            "coverage": ratio(len(valid_rows), expected_per_batch),
        })

    latest_topics = []
    for topic_id in dict.fromkeys(row["topic_id"] for row in questions.values()):
        topic_questions = [row for row in questions.values() if row["topic_id"] == topic_id]
        question_ids = {row["question_id"] for row in topic_questions}
        topic_latest = [row for row in canonical if row["question_id"] in question_ids and batch_id(row) == latest_batch]
        topic_valid = [row for row in topic_latest if eligible(row)]
        expected = len(question_ids) * len(channels)
        target_recommended = sum(any(
            mention["brand_id"] == target and mention["mention_context"] == "recommended"
            for mention in mentions[row["observation_id"]]
        ) for row in topic_valid)
        by_channel = []
        for channel in channels:
            channel_rows = [row for row in topic_valid if row["channel"] == channel]
            by_channel.append({
                "channel": channel,
                "coverage": ratio(len(channel_rows), len(question_ids)),
                "target_mentions": ratio(sum(target_mentioned(row) for row in channel_rows), len(channel_rows)),
                "observation_ids": [row["observation_id"] for row in channel_rows],
            })
        latest_topics.append({
            "topic_id": topic_id,
            "topic_label": topic_questions[0]["topic_label"],
            "question_ids": sorted(question_ids),
            "importance_mean": sum(row["importance_score"] for row in topic_questions) / len(topic_questions),
            "expected_observations": expected,
            "coverage": ratio(len(topic_valid), expected),
            "metrics": metrics(topic_valid),
            "target_recommended": ratio(target_recommended, len(topic_valid)),
            "target_owned_citation_responses": ratio(owned_citation_count(topic_valid), len(topic_valid)),
            "target_owned_citation_responses_with_domain_candidates": ratio(owned_citation_count(topic_valid, True), len(topic_valid)),
            "competitor_and_target_mention_counts": dict(sorted(Counter(
                mention["brand_id"] for row in topic_valid for mention in mentions[row["observation_id"]]
            ).items())),
            "source_citation_counts": dict(sorted(Counter(
                citation["source_id"] for row in topic_valid for citation in citations[row["observation_id"]]
            ).items())),
            "channels": by_channel,
            "excluded": [
                {field: row[field] for field in ("observation_id", "question_id", "channel", "collection_status", "quality_flag")}
                for row in topic_latest if not eligible(row)
            ],
            "prior_snapshots": [
                {"batch_id": batch, **metrics([
                    row for row in valid if row["question_id"] in question_ids and batch_id(row) == batch
                ])} for batch in batches[:-1]
            ],
        })

    excluded_by_reason = {
        "partial": [row["observation_id"] for row in canonical if row["collection_status"] == "partial"],
        "failed": [row["observation_id"] for row in canonical if row["collection_status"] == "failed"],
        "complete_language_mismatch": [
            row["observation_id"] for row in canonical
            if row["collection_status"] == "complete" and not eligible(row)
        ],
    }
    missing_title = [row["source_id"] for row in sources.values() if not row["title"]]
    missing_owned_brand = [row["source_id"] for row in sources.values() if row["is_brand_owned"] and not row["brand_id"]]
    return {
        "audit_version": 1,
        "dataset": {"name": manifest["dataset_name"], "version": manifest["version"], "fictional": manifest["fictional"], "target_brand_id": target},
        "policies": {
            "canonical_key": ["question_id", "channel", "source_snapshot_id"],
            "missing_snapshot_fallback": "Use date:YYYY-MM-DD from collected_at in its recorded timezone. The date identifies the available collection round when no snapshot ID exists. Snapshot/date namespaces remain separate; this does not guess that a missing-snapshot row belongs to a populated snapshot. Production ingestion should supply a stable collection_run_id.",
            "canonical_selection": "Prefer complete > partial > failed, then requested language match, then latest collected_at, then lexicographically greatest observation_id. Preserve every attempt and the canonical mapping. possible_duplicate alone does not disqualify a row.",
            "metric_eligibility": "One canonical row per question/channel/batch; complete and response_language matches questions.language. Exclude partial, failed and language-mismatch rows from mention/citation denominators; show their coverage separately. Partial evidence remains inspectable but absence is not a negative observation.",
            "summary_authority": "Recompute target_mentioned, mention_count and citation_count from mentions/citations; retain reported values in the audit.",
            "source_metadata": "Missing title uses domain or URL for display. An external source with null brand_id is valid. An owned source with null brand_id remains unresolved in authoritative metrics; domain-match attribution is a separate candidate metric.",
            "similar_questions": "Keep distinct question_id values; semantic similarity is not proof of duplicate collection. Do not sum synthetic interest as unique market demand.",
            "interpretation": "Observed mentions/citations are associations in synthetic data, not proof of content quality, actual product capability, causal effect, market demand, or future improvement.",
        },
        "record_counts": {name: len(rows) for name, rows in tables.items()},
        "integrity": {
            "duplicate_primary_keys": pk_issues,
            "invalid_foreign_keys": fk_issues,
            "exact_duplicate_question_queries": len(questions) - len({row["query"] for row in questions.values()}),
            "exact_duplicate_source_urls": len(sources) - len({row["url"] for row in sources.values()}),
        },
        "raw_quality": {
            "collection_status": dict(sorted(Counter(row["collection_status"] for row in observations).items())),
            "quality_flag": dict(sorted(Counter(row["quality_flag"] or "none" for row in observations).items())),
            "missing_snapshot_observation_ids": [row["observation_id"] for row in observations if not row.get("source_snapshot_id")],
        },
        "duplicates": {"group_count": len(duplicates), "excess_observation_count": len(observations) - len(canonical), "groups": duplicates},
        "summary_mismatches": mismatches,
        "metric_stages": {
            "raw": metrics(observations),
            "canonical": metrics(canonical),
            "canonical_complete": metrics([row for row in canonical if row["collection_status"] == "complete"]),
            "eligible": metrics(valid),
        },
        "excluded_canonical_observations": {reason: {"count": len(ids), "observation_ids": ids} for reason, ids in excluded_by_reason.items()},
        "source_metadata": {
            "missing_title": {"count": len(missing_title), "source_ids": missing_title},
            "null_brand_id_count": sum(row["brand_id"] is None for row in sources.values()),
            "external_null_brand_id_count": sum(not row["is_brand_owned"] and row["brand_id"] is None for row in sources.values()),
            "owned_missing_brand_id": {"count": len(missing_owned_brand), "source_ids": missing_owned_brand},
            "domain_attribution_candidates": ownership_candidates,
            "eligible_target_owned_citation_responses": owned_citation_count(valid),
            "eligible_target_owned_citation_responses_with_domain_candidates": owned_citation_count(valid, True),
        },
        "snapshots": snapshot_metrics,
        "latest_batch_id": latest_batch,
        "latest_topics": latest_topics,
    }


def resolve_from_root(path):
    value = Path(path)
    return value if value.is_absolute() else ROOT / value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="VIVI_MVP_Sample_Dataset")
    parser.add_argument("--output", help="JSON output path relative to the repository root; stdout when omitted")
    args = parser.parse_args()
    result = audit(resolve_from_root(args.dataset))
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = resolve_from_root(args.output)
        dataset_dir = resolve_from_root(args.dataset).resolve()
        if output.resolve().is_relative_to(dataset_dir):
            parser.error("--output must be outside the read-only source dataset directory")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
        print("Audit written to " + str(output))
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
