"""불완전한 제공 데이터로 통합 검증한다. 모델 호출은 없다."""

from copy import deepcopy
import json
from pathlib import Path
import unittest

from vivi_agent.evidence import EvidenceRepository


DATASET = Path(__file__).resolve().parents[1] / "VIVI_MVP_Sample_Dataset"


class EvidenceRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = EvidenceRepository(DATASET)

    def test_recollections_do_not_inflate_automation(self):
        pack = self.repo.build("automation")
        self.assertEqual(pack["quality"]["eligible"], 15)
        self.assertEqual(pack["quality"]["superseded_attempts"], 2)
        ids = {row["observation_id"] for row in pack["observations"]}
        self.assertTrue({"OBS0455", "OBS0456"}.issubset(ids))
        self.assertTrue({"OBS0118", "OBS0107"}.isdisjoint(ids))
        self.assertEqual(pack["metrics"]["topic.target_mentions"]["numerator"], 15)
        self.assertEqual(pack["metrics"]["topic.target_mentions"]["denominator"], 15)

    def test_latest_topic_totals_match_normalized_dataset(self):
        questions = json.loads((DATASET / "questions.json").read_text())
        topics = sorted({row["topic_id"] for row in questions})
        packs = [self.repo.build(topic) for topic in topics]
        self.assertEqual(sum(pack["quality"]["expected"] for pack in packs), 225)
        self.assertEqual(sum(pack["quality"]["eligible"] for pack in packs), 212)
        self.assertEqual(sum(pack["metrics"]["topic.target_mentions"]["numerator"] for pack in packs), 63)
        self.assertEqual(sum(pack["quality"]["superseded_attempts"] for pack in packs), 3)

    def test_missing_owned_brand_is_not_silently_inferred(self):
        pack = self.repo.build("automation")
        source = next(row for row in pack["sources"] if row["source_id"] == "SRC024")
        self.assertTrue(source["is_brand_owned"])
        self.assertIsNone(source["brand_id"])
        self.assertEqual(pack["metrics"]["source.SRC024.citation_responses"]["numerator"], 4)
        self.assertEqual(pack["metrics"]["topic.target_owned_citations"]["numerator"], 4)
        self.assertEqual(pack["metrics"]["brand.BR001.owned_citations"]["numerator"], 4)

    def test_brand_owned_citation_comparison_counts_responses_not_links(self):
        pack = self.repo.build("lead")
        expected_counts = {"BR001": 2, "BR002": 2, "BR003": 7, "BR004": 1, "BR005": 2}
        for brand_id, expected in expected_counts.items():
            with self.subTest(brand_id=brand_id):
                metric_id = f"brand.{brand_id}.owned_citations"
                metric = pack["metrics"][metric_id]
                self.assertEqual((metric["numerator"], metric["denominator"]), (expected, 15))
                self.assertEqual(len(metric["supporting_observation_ids"]), expected)
                self.assertEqual(len(set(metric["supporting_observation_ids"])), expected)
                self.assertEqual(self.repo.lookup(pack, [metric_id])[0]["numerator"], expected)
        target = pack["metrics"]["brand.BR001.owned_citations"]
        self.assertEqual(set(target["supporting_observation_ids"]), {"OBS0394", "OBS0401"})
        # OBS0401은 OrbitCRM 출처 2개를 인용하지만 응답은 1개로 집계한다.
        row = next(row for row in pack["observations"] if row["observation_id"] == "OBS0401")
        self.assertEqual(sum(c["source"]["brand_id"] == "BR001" for c in row["citations"]), 2)
        self.assertEqual(target["numerator"], pack["metrics"]["brand.BR005.owned_citations"]["numerator"])

    def test_scale_uses_nine_valid_observations(self):
        pack = self.repo.build("scale")
        self.assertEqual(pack["quality"]["eligible"], 9)
        self.assertEqual(pack["quality"]["expected"], 15)
        self.assertEqual(pack["quality"]["coverage"], 0.6)
        self.assertEqual(pack["metrics"]["topic.target_mentions"]["numerator"], 3)
        self.assertEqual(pack["metrics"]["topic.target_mentions"]["denominator"], 9)
        self.assertEqual(sum(row["reason"] == "failed" for row in pack["excluded"]), 3)
        self.assertEqual(sum(row["reason"] == "partial" for row in pack["excluded"]), 3)

    def test_security_source_citation_is_not_brand_recommendation(self):
        pack = self.repo.build("security")
        self.assertEqual(pack["metrics"]["topic.target_mentions"]["numerator"], 0)
        owned = pack["metrics"]["topic.target_owned_citations"]
        self.assertEqual((owned["numerator"], owned["denominator"]), (6, 12))
        self.assertEqual(pack["quality"]["by_channel"]["Perplexity"]["eligible"], 2)
        self.assertEqual(pack["capabilities"], {"body_access": False})

    def test_lead_question_gap_has_all_three_denominator_evidence_rows(self):
        pack = self.repo.build("lead")
        metric = pack["metrics"]["question.Q068.target_mentions"]
        self.assertEqual((metric["numerator"], metric["denominator"]), (0, 3))
        self.assertEqual(set(metric["observation_ids"]), {"OBS0406", "OBS0407", "OBS0408"})
        self.assertEqual(metric["supporting_observation_ids"], [])
        self.assertEqual(pack["metrics"]["channel.Perplexity.target_mentions"]["denominator"], 5)

    def test_matched_comparison_excludes_invalid_counterpart(self):
        pack = self.repo.build("security")
        comparison = pack["previous_comparison"]
        # 이전 Q026/Perplexity가 부분 수집됐고, 최신 회차에는 다른 셀 3개가 부분 수집됐다.
        self.assertEqual(comparison["matched_count"], 11)
        self.assertEqual(comparison["previous_target_mentions"]["denominator"], 11)
        self.assertEqual(comparison["current_target_mentions"]["denominator"], 11)
        self.assertNotIn("OBS0156", comparison["current_target_mentions"]["observation_ids"])
        previous = {row["observation_id"]: row for row in comparison["previous_observations"]}
        current = {row["observation_id"]: row for row in pack["observations"]}
        for pair in comparison["pairs"]:
            left, right = previous[pair["previous_observation_id"]], current[pair["current_observation_id"]]
            self.assertEqual((left["question_id"], left["channel"]), (right["question_id"], right["channel"]))

    def test_summary_mismatch_uses_child_rows(self):
        pack = self.repo.build("retention")
        row = next(row for row in pack["observations"] if row["observation_id"] == "OBS0388")
        self.assertTrue(row["target_mentioned"])
        self.assertFalse(row["recomputed"]["target_mentioned"])
        self.assertEqual(pack["metrics"]["topic.target_mentions"]["numerator"], 6)

    def test_language_mismatch_does_not_count_as_absence(self):
        pack = self.repo.build("analytics")
        self.assertEqual(pack["quality"]["eligible"], 14)
        self.assertEqual(pack["quality"]["superseded_attempts"], 1)
        self.assertEqual([row["observation_id"] for row in pack["excluded"] if row["reason"] == "language_mismatch"], ["OBS0131"])

    def test_context_is_preserved_hashed_and_detached(self):
        base = self.repo.build("lead")
        context = {"business_goal": "qualified demos", "constraints": {"team_size": 2}}
        supplied = deepcopy(context)
        pack = self.repo.build("lead", context=context)
        self.assertEqual(context, supplied)
        for key, value in context.items():
            self.assertEqual(pack["business_context"][key], value)
        self.assertEqual(pack["business_context"]["verified_product_facts_status"], "unknown")
        self.assertEqual(pack["business_context"]["verified_product_facts"], [])
        self.assertNotEqual(base["context_hash"], pack["context_hash"])
        self.assertEqual(base["dataset_hash"], pack["dataset_hash"])
        reordered = {"constraints": {"team_size": 2}, "business_goal": "qualified demos"}
        self.assertEqual(pack["context_hash"], self.repo.build("lead", context=reordered)["context_hash"])
        pack["business_context"]["constraints"]["team_size"] = 999
        self.assertEqual(context["constraints"]["team_size"], 2)

    def test_reference_lookup_is_scoped_and_read_only(self):
        pack = self.repo.build("lead")
        found = self.repo.lookup(pack, ["Q068", "OBS0408", "C00984", "SRC025", "question.Q068.target_mentions"])
        self.assertEqual([row["kind"] for row in found], ["question", "observation", "citation", "source_metadata", "metric"])
        found[1]["response_text"] = "mutated"
        self.assertNotEqual(self.repo.lookup(pack, ["OBS0408"])[0]["response_text"], "mutated")
        with self.assertRaises(ValueError):
            self.repo.lookup(pack, ["OBS0156"])
        self.assertTrue(all(
            pair["previous_observation_id"] in pack["allowed_evidence_ids"]
            for pair in pack["previous_comparison"]["pairs"]
        ))

    def test_first_snapshot_does_not_invent_comparison(self):
        pack = self.repo.build("lead", "SNAP-1")
        self.assertIsNone(pack["previous_comparison"]["previous_snapshot_id"])
        self.assertEqual(pack["previous_comparison"]["matched_count"], 0)
        self.assertEqual(pack["metrics"]["comparison.previous.target_mentions"]["denominator"], 0)

    def test_unknown_scope_rejected(self):
        with self.assertRaises(ValueError):
            self.repo.build("does-not-exist")
        with self.assertRaises(ValueError):
            self.repo.build("lead", "SNAP-999")


if __name__ == "__main__":
    unittest.main()
