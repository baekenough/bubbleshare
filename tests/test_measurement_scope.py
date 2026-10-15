"""제공 데이터로 측정 계약과 대응 표본의 후속 평가를 검증한다."""

from copy import deepcopy
from pathlib import Path
import unittest

from vivi_agent.evidence import EvidenceRepository
from vivi_agent.measurement import compile_measurement, evaluate_follow_up, render_measurement


DATASET = Path(__file__).resolve().parents[1] / "VIVI_MVP_Sample_Dataset"


def lead_action():
    return {
        "question_ids": ["Q068"], "channels": ["ChatGPT", "Gemini", "Perplexity"],
        "target_asset": {"mode": "verify_then_choose", "source_ids": ["SRC003", "SRC022", "SRC001"]},
        "measurement": {
            "baseline_metric_ids": ["question.Q068.target_mentions", "question.Q068.target_recommendations", "question.Q068.target_owned_citations"],
            "secondary_metric_ids": ["channel.Perplexity.target_mentions", "channel.Perplexity.target_owned_citations"],
        },
    }


class MeasurementScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = EvidenceRepository(DATASET).build("lead")

    def setUp(self):
        self.action = lead_action()

    def test_q068_and_perplexity_have_distinct_denominators_and_full_sampling(self):
        contract = compile_measurement(self.action, self.pack)
        self.assertEqual(len(contract["primary_metrics"]), 3)
        for metric in contract["primary_metrics"]:
            self.assertEqual((metric["numerator"], metric["denominator"]), (0, 3))
            self.assertEqual({p["question_id"] for p in metric["question_channel_pairs"]}, {"Q068"})
            self.assertEqual(set(metric["baseline_observation_ids"]), {"OBS0406", "OBS0407", "OBS0408"})
        for metric in contract["secondary_metrics"]:
            self.assertEqual((metric["numerator"], metric["denominator"]), (0, 5))
            self.assertEqual({p["question_id"] for p in metric["question_channel_pairs"]}, {"Q066", "Q067", "Q068", "Q069", "Q070"})
            self.assertEqual({p["channel"] for p in metric["question_channel_pairs"]}, {"Perplexity"})
        self.assertEqual(len(contract["sampling_plan"]), 7)
        self.assertEqual(contract["cadence"]["days_after_publication"], [7, 14])
        self.assertEqual(contract["cadence"]["total_scheduled_observations"], 14)
        self.assertEqual(contract["cadence"]["collections_per_pair_per_scheduled_day"], 1)

    def test_wider_metric_cannot_be_silently_reclassified_from_primary(self):
        self.action["measurement"]["baseline_metric_ids"].append("channel.Perplexity.target_recommendations")
        with self.assertRaisesRegex(ValueError, "Primary metric"):
            compile_measurement(self.action, self.pack)

    def test_foreign_question_cannot_be_primary(self):
        self.action["measurement"]["baseline_metric_ids"] = ["question.Q066.target_mentions"]
        with self.assertRaisesRegex(ValueError, "Primary metric"):
            compile_measurement(self.action, self.pack)

    def test_three_channel_metric_cannot_be_used_for_a_one_channel_action(self):
        self.action["channels"] = ["Perplexity"]
        with self.assertRaisesRegex(ValueError, "channel scope"):
            compile_measurement(self.action, self.pack)

    def test_every_explicit_metric_is_preserved_with_its_original_denominator(self):
        self.action["measurement"]["secondary_metric_ids"].append("topic.target_mentions")
        contract = compile_measurement(self.action, self.pack)
        chosen = [*self.action["measurement"]["baseline_metric_ids"], *self.action["measurement"]["secondary_metric_ids"]]
        compiled = [*contract["primary_metrics"], *contract["secondary_metrics"]]
        self.assertEqual([m["metric_id"] for m in compiled], chosen)
        self.assertEqual(compiled[-1]["denominator"], 15)
        self.assertEqual(len(contract["sampling_plan"]), 15)

    def test_unknown_metric_is_rejected(self):
        self.action["measurement"]["secondary_metric_ids"].append("channel.DoesNotExist.target_mentions")
        with self.assertRaisesRegex(ValueError, "Unknown baseline"):
            compile_measurement(self.action, self.pack)

    def test_historical_metric_is_not_a_publication_baseline(self):
        self.action["measurement"]["secondary_metric_ids"] = ["comparison.previous.target_mentions"]
        with self.assertRaisesRegex(ValueError, "current eligible"):
            compile_measurement(self.action, self.pack)

    def test_empty_primary_and_missing_scope_are_rejected(self):
        for field in ("question_ids", "channels"):
            with self.subTest(field=field):
                bad = deepcopy(self.action)
                bad[field] = []
                with self.assertRaises(ValueError):
                    compile_measurement(bad, self.pack)
        self.action["measurement"]["baseline_metric_ids"] = []
        with self.assertRaisesRegex(ValueError, "baseline_metric_ids"):
            compile_measurement(self.action, self.pack)

    def test_secondary_selection_is_explicit_and_disjoint(self):
        del self.action["measurement"]["secondary_metric_ids"]
        with self.assertRaisesRegex(ValueError, "explicit list"):
            compile_measurement(self.action, self.pack)
        self.action["measurement"]["secondary_metric_ids"] = ["question.Q068.target_mentions"]
        with self.assertRaisesRegex(ValueError, "both primary and secondary"):
            compile_measurement(self.action, self.pack)

    def test_unknown_action_scope_is_rejected(self):
        self.action["question_ids"] = ["Q999"]
        with self.assertRaisesRegex(ValueError, "unknown question/channel"):
            compile_measurement(self.action, self.pack)

    def test_corrupted_denominator_is_rejected_without_repairing_it(self):
        pack = deepcopy(self.pack)
        pack["metrics"]["question.Q068.target_mentions"]["denominator"] = 5
        with self.assertRaisesRegex(ValueError, "inconsistent denominator"):
            compile_measurement(self.action, pack)

    def test_source_selection_and_new_asset_registration_are_explicit(self):
        contract = compile_measurement(self.action, self.pack)
        check = contract["source_citation_check"]
        self.assertEqual(check["source_ids"], ["SRC003", "SRC022", "SRC001"])
        self.assertTrue(check["requires_final_source_selection"])
        self.assertTrue(check["new_asset_requires_source_id_registration"])
        self.assertEqual(check["status"], "pending_asset_selection_or_registration")
        self.assertEqual(check["baseline_citing_observation_ids"], [])
        self.action["target_asset"] = {"mode": "create", "source_ids": []}
        created = compile_measurement(self.action, self.pack)["source_citation_check"]
        self.assertTrue(created["new_asset_requires_source_id_registration"])
        self.assertEqual(created["source_ids"], [])
        self.assertNotEqual(created["status"], "ready")

    def test_competitor_or_unresolved_owner_cannot_be_the_selected_source(self):
        for source_id in ("SRC025", "SRC024", "SRC999"):
            with self.subTest(source=source_id):
                self.action["target_asset"]["source_ids"] = [source_id]
                with self.assertRaisesRegex(ValueError, "explicitly target-owned"):
                    compile_measurement(self.action, self.pack)

    def test_compilation_does_not_mutate_evidence_or_action(self):
        pack, action = deepcopy(self.pack), deepcopy(self.action)
        contract = compile_measurement(action, pack)
        contract["scope"]["topic_id"] = "mutated"
        contract["primary_metrics"][0]["baseline_observation_ids"].append("bad")
        self.assertEqual(pack, self.pack)
        self.assertEqual(action, self.action)

    def test_unchanged_zero_exposure_is_not_a_positive_signal(self):
        contract = compile_measurement(self.action, self.pack)
        result = evaluate_follow_up(contract, self.pack["observations"])
        self.assertFalse(result["positive_signal"])
        self.assertFalse(result["causal_effect_established"])
        self.assertTrue(all(m["status"] == "unchanged" for m in result["primary_metrics"]))
        self.assertTrue(all((m["numerator"], m["denominator"]) == (0, 3) for m in result["primary_metrics"]))

    def test_partial_follow_up_is_missing_not_zero_exposure(self):
        contract = compile_measurement(self.action, self.pack)
        rows = deepcopy(self.pack["observations"])
        next(row for row in rows if row["observation_id"] == "OBS0408")["collection_status"] = "partial"
        result = evaluate_follow_up(contract, rows)
        self.assertFalse(result["positive_signal"])
        for metric in result["primary_metrics"]:
            self.assertEqual(metric["status"], "insufficient_data")
            self.assertEqual((metric["denominator"], metric["expected_denominator"]), (2, 3))
            self.assertIsNone(metric["numerator_change"])
        self.assertIn({"observation_id": "OBS0408", "reason": "partial"}, result["excluded"])

    def test_failed_and_wrong_language_are_also_missing(self):
        contract = compile_measurement(self.action, self.pack)
        rows = deepcopy(self.pack["observations"])
        next(row for row in rows if row["observation_id"] == "OBS0406")["collection_status"] = "failed"
        next(row for row in rows if row["observation_id"] == "OBS0407")["response_language"] = "en"
        result = evaluate_follow_up(contract, rows)
        self.assertEqual(result["primary_metrics"][0]["denominator"], 1)
        self.assertEqual({r["reason"] for r in result["excluded"]}, {"failed", "language_mismatch"})

    def test_recollection_prefers_complete_and_counts_each_pair_once(self):
        contract = compile_measurement(self.action, self.pack)
        rows = deepcopy(self.pack["observations"])
        duplicate = deepcopy(next(row for row in rows if row["observation_id"] == "OBS0408"))
        duplicate.update(observation_id="OBS9999", collection_status="partial", collected_at="2030-01-01T00:00:00+09:00")
        rows.append(duplicate)
        result = evaluate_follow_up(contract, rows)
        self.assertEqual(result["primary_metrics"][0]["denominator"], 3)
        self.assertEqual(result["primary_metrics"][0]["status"], "unchanged")
        self.assertIn({"observation_id": "OBS9999", "reason": "superseded"}, result["excluded"])

    def test_secondary_only_increase_is_not_primary_success(self):
        contract = compile_measurement(self.action, self.pack)
        rows = deepcopy(self.pack["observations"])
        row = next(row for row in rows if row["question_id"] == "Q066" and row["channel"] == "Perplexity")
        row["mentions"].append({"brand_id": "BR001", "mention_context": "recommended"})
        result = evaluate_follow_up(contract, rows)
        self.assertEqual(result["secondary_metrics"][0]["status"], "improved")
        self.assertFalse(result["positive_signal"])

    def test_primary_increase_is_descriptive_signal_with_complete_paired_data(self):
        contract = compile_measurement(self.action, self.pack)
        rows = deepcopy(self.pack["observations"])
        row = next(row for row in rows if row["question_id"] == "Q068" and row["channel"] == "ChatGPT")
        row["mentions"].extend([{"brand_id": "BR001", "mention_context": "recommended"}] * 2)
        result = evaluate_follow_up(contract, rows)
        self.assertTrue(result["positive_signal"])
        self.assertEqual(result["primary_metrics"][0]["numerator"], 1)
        self.assertFalse(result["causal_effect_established"])

    def test_mixed_follow_up_rounds_cannot_be_merged(self):
        contract = compile_measurement(self.action, self.pack)
        rows = deepcopy(self.pack["observations"])
        next(row for row in rows if row["observation_id"] == "OBS0408")["source_snapshot_id"] = "FOLLOW-UP-2"
        with self.assertRaisesRegex(ValueError, "one follow-up snapshot"):
            evaluate_follow_up(contract, rows)


class MeasurementRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pack = EvidenceRepository(DATASET).build("lead")

    def setUp(self):
        self.action = lead_action()
        self.action["question_ids"] = ["Q069"]
        self.action["measurement"]["baseline_metric_ids"] = [
            "question.Q069.target_mentions", "question.Q069.target_recommendations", "question.Q069.target_owned_citations",
        ]
        self.action["target_asset"]["source_ids"] = ["SRC022"]
        self.contract = compile_measurement(self.action, self.pack)

    def test_q069_and_perplexity_scopes_are_rendered_in_full(self):
        rendered = render_measurement(self.contract)
        follow_up = rendered["follow_up"]
        self.assertIn("question.Q069.target_mentions 기준선 1/3", follow_up)
        self.assertIn("question.Q069.target_recommendations 기준선 1/3", follow_up)
        self.assertIn("question.Q069.target_owned_citations 기준선 0/3", follow_up)
        self.assertIn("channel.Perplexity.target_mentions 기준선 0/5", follow_up)
        self.assertIn("channel.Perplexity.target_owned_citations 기준선 0/5", follow_up)
        for question in ("Q066", "Q067", "Q068", "Q069", "Q070"):
            self.assertIn(f"{question}/Perplexity", follow_up)
        for channel in ("ChatGPT", "Gemini", "Perplexity"):
            self.assertIn(f"Q069/{channel}", follow_up)
        self.assertIn("회차별 고정 수집 범위는 7페어", follow_up)
        self.assertIn("총 14개 응답", follow_up)
        self.assertIn("확정 게시일을 기준으로 7일 후·14일 후 각각 1회", follow_up)

    def test_positive_signal_requires_primary_increase_from_actual_baseline(self):
        positive = render_measurement(self.contract)["positive_signal"]
        self.assertIn("question.Q069.target_mentions 1/3 → 2/3 이상", positive)
        self.assertIn("question.Q069.target_recommendations 1/3 → 2/3 이상", positive)
        self.assertIn("question.Q069.target_owned_citations 0/3 → 1/3 이상", positive)
        self.assertNotIn("channel.Perplexity", positive)
        self.assertIn("보조 지표의 개선만으로는 액션 성공으로 처리하지 않습니다", positive)
        self.assertIn("0 노출 유지도 긍정 신호가 아닙니다", positive)

    def test_missing_data_and_ineligible_observations_have_explicit_outcomes(self):
        rendered = render_measurement(self.contract)
        follow_up, positive, stop = (rendered[key] for key in ("follow_up", "positive_signal", "stop_or_revise"))
        self.assertIn("complete이면서 질문 언어와 response_language가 일치", follow_up)
        for exclusion in ("superseded", "partial", "failed", "language_mismatch"):
            self.assertIn(exclusion, follow_up)
        self.assertIn("실제 관측 분모를 표시", follow_up)
        self.assertIn("insufficient_data", follow_up)
        self.assertIn("insufficient_data", positive)
        self.assertIn("효과 결론을 내리지 말고 해당 질문/채널을 추가 수집", stop)
        self.assertIn("보조 지표의 결측은 해당 보조 지표의 판정을 보류", stop)

    def test_pending_asset_registration_explicitly_holds_follow_up(self):
        rendered = render_measurement(self.contract)
        for key in ("follow_up", "stop_or_revise"):
            self.assertIn("pending_asset_selection_or_registration", rendered[key])
            self.assertIn("후보: SRC022", rendered[key])
            self.assertIn("게시 전에 최종 자산의 정확한 URL과 자사 소유 source ID 등록을 완료", rendered[key])
            self.assertIn("등록 요청만으로 완료 처리하지 않", rendered[key])
            self.assertIn("해결될 때까지 후속 수집을 보류", rendered[key])

    def test_known_revised_asset_and_empty_secondary_have_correct_branches(self):
        self.action["target_asset"]["mode"] = "revise"
        self.action["measurement"]["secondary_metric_ids"] = []
        contract = compile_measurement(self.action, self.pack)
        rendered = render_measurement(contract)
        self.assertIn("보조 지표는 선택하지 않았습니다", rendered["follow_up"])
        self.assertIn("회차별 고정 수집 범위는 3페어", rendered["follow_up"])
        self.assertIn("총 6개 응답", rendered["follow_up"])
        self.assertNotIn("pending_asset_selection_or_registration", rendered["follow_up"])
        self.assertIn("선택한 기존 source ID(SRC022)", rendered["follow_up"])

    def test_renderer_is_a_detached_view_with_exactly_the_three_prose_fields(self):
        before = deepcopy(self.contract)
        first = render_measurement(self.contract)
        self.assertEqual(set(first), {"follow_up", "positive_signal", "stop_or_revise"})
        self.assertEqual(first, render_measurement(self.contract))
        self.assertEqual(self.contract, before)


if __name__ == "__main__":
    unittest.main()
