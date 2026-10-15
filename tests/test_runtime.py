import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from vivi_agent.ledger import BudgetExceeded, InFlightError, Ledger
from vivi_agent.provider import OpenAIProvider, ProviderError


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def test_failed_attempts_are_charged_and_run_cap_survives_restart(self):
        ledger = Ledger(self.path)
        for attempt in range(5):
            receipt = ledger.reserve("run", "diagnose", f"prompt-{attempt}")
            ledger.fail(receipt, "provider unavailable")
        reopened = Ledger(self.path)
        self.assertEqual(reopened.stats("run")["failed"], 5)
        self.assertEqual(reopened.stats("run")["charged_cost_usd"], .09)
        self.assertEqual(reopened.remaining("run"), 0)
        with self.assertRaises(BudgetExceeded):
            reopened.reserve("run", "diagnose", "another")

    def test_completed_result_is_reused_without_another_charge(self):
        ledger = Ledger(self.path)
        receipt = ledger.reserve("run", "diagnose", "hash")
        ledger.complete(receipt, {"decision": "observe"}, {"actual_provider_calls": 1})
        reopened = Ledger(self.path)
        self.assertEqual(reopened.get_completed("run", "diagnose", "hash"), {"decision": "observe"})
        self.assertEqual(reopened.reserve("run", "diagnose", "hash"), receipt)
        self.assertEqual(reopened.stats("run")["calls"], 1)

    def test_uncertain_inflight_call_blocks_same_stage_and_remains_charged(self):
        ledger = Ledger(self.path)
        ledger.reserve("run", "diagnose", "hash")
        with self.assertRaises(InFlightError):
            Ledger(self.path).reserve("run", "diagnose", "hash")
        self.assertEqual(ledger.stats("run")["charged_cost_usd"], .018)

    def test_concurrent_reservations_cannot_exceed_monthly_cap(self):
        ledger = Ledger(self.path, monthly_call_cap=5)

        def reserve(index):
            try:
                return ledger.reserve(f"run-{index}", "diagnose", "hash")
            except BudgetExceeded:
                return None

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(reserve, range(20)))
        self.assertEqual(sum(result is not None for result in results), 5)
        self.assertEqual(ledger.stats("run-0")["monthly_calls"], 5)

    def test_concurrent_same_input_has_one_owner(self):
        ledger = Ledger(self.path)

        def reserve(_):
            try:
                return ledger.reserve("run", "diagnose", "hash")
            except InFlightError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(8)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_money_cap_includes_fixed_cost_and_reserved_attempts(self):
        ledger = Ledger(self.path, max_monthly_cogs=10.554)
        for index in range(3):
            ledger.reserve(f"run-{index}", "diagnose", "hash")
        with self.assertRaises(BudgetExceeded):
            ledger.reserve("run-4", "diagnose", "hash")
        self.assertEqual(ledger.stats("run-0")["monthly_total_cogs_usd"], 10.554)

    def test_month_rollover_resets_monthly_usage_but_not_bundle_attempts(self):
        now = [datetime(2026, 10, 31, tzinfo=timezone.utc)]
        ledger = Ledger(self.path, monthly_call_cap=1, max_attempts=1, clock=lambda: now[0])
        receipt = ledger.reserve("old-run", "diagnose", "hash")
        ledger.fail(receipt, "timeout")
        now[0] = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.assertEqual(ledger.stats("old-run")["monthly_calls"], 0)
        self.assertEqual(ledger.remaining("old-run"), 0)
        self.assertEqual(ledger.remaining("new-run"), 1)
        ledger.reserve("new-run", "diagnose", "hash")

    def test_pair_is_atomic_and_idempotent_after_binding_and_restart(self):
        ledger = Ledger(self.path)
        for stage in ("diagnose", "design", "review"):
            receipt = ledger.reserve("run", stage, "hash")
            ledger.complete(receipt, {"stage": stage}, {})
        pair = ledger.reserve_pair("run")
        self.assertEqual(set(pair), {"revise", "re_review"})
        self.assertEqual(ledger.remaining("run"), 0)
        self.assertEqual(ledger.stats("run")["calls"], 5)
        self.assertEqual(ledger.stats("run")["attempted_calls"], 3)
        ledger.bind_reservation(pair["revise"], "revise", "revision-hash")
        ledger.complete(pair["revise"], {"revised": True}, {})
        reopened = Ledger(self.path)
        self.assertEqual(reopened.reserve_pair("run"), pair)
        self.assertEqual(reopened.get_completed("run", "revise", "revision-hash"), {"revised": True})
        reopened.bind_reservation(pair["re_review"], "re_review", "review-hash")
        reopened.complete(pair["re_review"], {"accepted": True}, {})
        self.assertEqual(reopened.stats("run")["charged_cost_usd"], .09)

    def test_pair_refuses_one_remaining_slot_without_partial_reservation(self):
        ledger = Ledger(self.path)
        for attempt in range(4):
            receipt = ledger.reserve("run", "step", str(attempt))
            ledger.fail(receipt, "failure")
        with self.assertRaises(BudgetExceeded):
            ledger.reserve_pair("run")
        self.assertEqual(ledger.stats("run")["calls"], 4)
        self.assertEqual(ledger.remaining("run"), 1)

    def test_concurrent_pairs_cannot_split_the_last_three_monthly_slots(self):
        ledger = Ledger(self.path, monthly_call_cap=3)

        def reserve(index):
            try:
                return ledger.reserve_pair(f"run-{index}")
            except BudgetExceeded:
                return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            pairs = list(pool.map(reserve, range(4)))
        self.assertEqual(sum(pair is not None for pair in pairs), 1)
        self.assertEqual(ledger.stats("run-0")["monthly_calls"], 2)

    def test_concurrent_same_pair_returns_the_same_two_slots(self):
        ledger = Ledger(self.path)
        with ThreadPoolExecutor(max_workers=8) as pool:
            pairs = list(pool.map(lambda _: ledger.reserve_pair("run"), range(8)))
        self.assertTrue(all(pair == pairs[0] for pair in pairs))
        self.assertEqual(ledger.stats("run")["calls"], 2)

    def test_only_unstarted_pair_slots_can_be_released(self):
        ledger = Ledger(self.path, monthly_call_cap=2)
        pair = ledger.reserve_pair("run")
        ledger.bind_reservation(pair["revise"], "revise", "hash")
        self.assertFalse(ledger.release(pair["revise"]))
        self.assertTrue(ledger.release(pair["re_review"]))
        self.assertFalse(ledger.release(pair["re_review"]))
        with self.assertRaises(InFlightError):
            ledger.bind_reservation(pair["revise"], "revise", "hash")
        ledger.fail(pair["revise"], "timeout")
        stats = ledger.stats("run")
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(stats["released"], 1)
        self.assertEqual(stats["charged_cost_usd"], .018)
        self.assertEqual(stats["monthly_calls"], 1)
        self.assertEqual(ledger.remaining("run"), 1)
        with self.assertRaises(BudgetExceeded):
            ledger.reserve_pair("run")

    def test_month_rollover_rechecks_funding_for_the_whole_unused_pair(self):
        now = [datetime(2026, 10, 31, tzinfo=timezone.utc)]
        ledger = Ledger(self.path, monthly_call_cap=3, clock=lambda: now[0])
        pair = ledger.reserve_pair("old-run")
        now[0] = datetime(2026, 11, 1, tzinfo=timezone.utc)
        ledger.reserve("other-1", "step", "hash")
        ledger.reserve("other-2", "step", "hash")
        with self.assertRaises(BudgetExceeded):
            ledger.bind_reservation(pair["revise"], "revise", "hash")
        self.assertEqual(ledger.stats("old-run")["attempted_calls"], 0)
        self.assertEqual(ledger.stats("old-run")["monthly_calls"], 2)


class ProviderTests(unittest.TestCase):
    def test_structured_response_records_usage_without_live_request(self):
        with tempfile.TemporaryDirectory() as temp:
            response = SimpleNamespace(
                id="resp_test", model="test-model", status="completed",
                output_text='{"decision":"observe"}', _request_id="req_test",
                model_dump=lambda **_: {"usage": {"input_tokens": 10, "output_tokens": 4}, "output": []},
            )
            client = SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=response)))
            provider = OpenAIProvider(model="test-model", client=client, artifact_dir=Path(temp))
            schema = {"type": "object", "properties": {"decision": {"type": "string"}}, "required": ["decision"], "additionalProperties": False}
            self.assertEqual(provider.generate("diagnose", "supplied evidence", schema), {"decision": "observe"})
            args = client.responses.create.call_args.kwargs
            self.assertEqual(args["text"]["format"]["schema"], schema)
            self.assertEqual(args["tools"], [])
            self.assertEqual(args["max_output_tokens"], 3000)
            self.assertFalse(args["store"])
            self.assertEqual(provider.last_receipt["actual_provider_calls"], 1)
            self.assertEqual(provider.last_receipt["usage"]["input_tokens"], 10)
            self.assertIsNone(provider.last_receipt["actual_cost_usd"])
            self.assertTrue(Path(provider.last_receipt["trace_path"]).exists())

    def test_provider_uses_stage_specific_output_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            response = SimpleNamespace(
                id="resp_test", model="test-model", status="completed",
                output_text='{}', model_dump=lambda **_: {"usage": {}, "output": []},
            )
            create = Mock(return_value=response)
            provider = OpenAIProvider(
                model="test-model", artifact_dir=Path(temp),
                client=SimpleNamespace(responses=SimpleNamespace(create=create)),
            )
            for stage, limit in {"diagnose": 3000, "design": 12000, "review": 3000,
                                 "revise": 6000, "re_review": 3000, "evaluate": 2500}.items():
                with self.subTest(stage=stage):
                    provider.generate(stage, "evidence", {"type": "object"})
                    self.assertEqual(create.call_args.kwargs["max_output_tokens"], limit)
                    self.assertEqual(provider.last_receipt["max_output_tokens"], limit)

    def test_provider_error_does_not_log_exception_text_or_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            create = Mock(side_effect=RuntimeError("invalid key: secret-test-sentinel"))
            provider = OpenAIProvider(
                model="test-model", artifact_dir=Path(temp),
                client=SimpleNamespace(responses=SimpleNamespace(create=create)),
            )
            with self.assertRaises(ProviderError) as caught:
                provider.generate("review", "supplied evidence", {"type": "object"})
            self.assertEqual(create.call_count, 1)
            self.assertNotIn("secret-test-sentinel", str(caught.exception))
            trace = Path(provider.last_receipt["trace_path"]).read_text()
            self.assertNotIn("secret-test-sentinel", trace)
            self.assertEqual(json.loads(trace)["receipt"]["status"], "failed")

    def test_incomplete_response_is_not_accepted_as_action_result(self):
        with tempfile.TemporaryDirectory() as temp:
            response = SimpleNamespace(
                id="resp_incomplete", model="test-model", status="incomplete",
                output_text='{"candidate":"partial"}',
                model_dump=lambda **_: {"usage": {"input_tokens": 10}, "output": []},
            )
            provider = OpenAIProvider(
                model="test-model", artifact_dir=Path(temp),
                client=SimpleNamespace(responses=SimpleNamespace(create=Mock(return_value=response))),
            )
            with self.assertRaises(ProviderError):
                provider.generate("design", "evidence", {"type": "object"})
            self.assertEqual(provider.last_receipt["status"], "failed")
            self.assertEqual(provider.last_receipt["usage"]["input_tokens"], 10)


if __name__ == "__main__":
    unittest.main()
