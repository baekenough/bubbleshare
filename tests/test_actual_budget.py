from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
import json
import sqlite3
import uuid
from zoneinfo import ZoneInfo

import pytest

from vivi_agent.actual_budget import (
    ActualBudget, ActualBudgetExceeded, ActualBudgetInFlight, ActualPriceUnknown,
    PRICE_CATALOG_ID, estimate_max_cost,
)
from vivi_agent.ledger import Ledger


NOW = datetime(2026, 10, 15, 12, tzinfo=ZoneInfo('Asia/Seoul'))


def quote(amount='0.2'):
    value = estimate_max_cost('openrouter', 'openai/gpt-5.4', 1000, 1000)
    return value | {'estimated_max_cost_usd': amount, 'prompt_hash': 'prompt-hash'}


def receipt(cost='0.03', **updates):
    return {'invocation_id': uuid.uuid4().hex, 'requested_model': 'openai/gpt-5.4',
            'prompt_hash': 'prompt-hash', 'actual_provider_calls': 1,
            'status': 'completed', 'actual_cost_usd': cost} | updates


def budget(tmp_path, **kwargs):
    return ActualBudget(tmp_path / 'actual.sqlite', clock=lambda: NOW, **kwargs)


def attempt(b, key='run', value=None):
    reservation = b.reserve(key, value or quote())
    b.mark_started(reservation)
    return reservation


@pytest.mark.parametrize('provider,model,expected', [
    ('openrouter', 'openai/gpt-5.4', '0.0175'),
    ('openai', 'gpt-5.4-2026-03-05', '0.0175'),
    ('openrouter', 'openai/gpt-5.4-mini', '0.00525'),
    ('openrouter', 'openai/gpt-5.4-nano', '0.00145'),
])
def test_pinned_uncached_standard_prices(provider, model, expected):
    q = estimate_max_cost(provider, model, 1000, 1000)
    assert Decimal(q['estimated_max_cost_usd']) == Decimal(expected)
    assert q['pricing_known'] is True and q['price_catalog_id'] == PRICE_CATALOG_ID


def test_long_context_price_multiplier_is_not_undercounted():
    q = estimate_max_cost('openrouter', 'openai/gpt-5.4', 272000, 1000)
    assert q['long_context_pricing'] is True
    assert Decimal(q['estimated_max_cost_usd']) == Decimal('1.3825')
    with pytest.raises(ActualPriceUnknown):
        estimate_max_cost('openrouter', 'openai/gpt-5.4-mini', 400000, 1)


@pytest.mark.parametrize('provider,model', [('other', 'gpt-5.4'), ('openrouter', 'openai/unknown'),
                                         ('openrouter', 'gpt-5.4'), ('openai', 'gpt-5.4:new')])
def test_unknown_price_fails_closed(provider, model):
    with pytest.raises(ActualPriceUnknown):
        estimate_max_cost(provider, model, 100, 100)


def test_settlement_releases_only_unused_reservation_and_is_idempotent(tmp_path):
    b = budget(tmp_path)
    reservation = attempt(b)
    assert b.stats()['held_cost_usd'] == '0.200000000'
    r = receipt()
    b.settle(reservation, r)
    b.settle(reservation, r)
    assert b.stats()['confirmed_actual_cost_usd'] == '0.030000000'
    assert b.stats()['held_cost_usd'] == '0.000000000'
    with pytest.raises(ActualBudgetInFlight):
        b.settle(reservation, r | {'actual_cost_usd': '0.01'})
    with pytest.raises(ActualBudgetInFlight):
        b.reserve('run', quote())


def test_known_failed_generation_is_settled_at_actual_cost(tmp_path):
    b = budget(tmp_path)
    b.settle(attempt(b), receipt('0.09', status='failed'))
    assert b.stats()['confirmed_actual_cost_usd'] == '0.090000000'
    assert b.stats()['held_calls'] == 0


@pytest.mark.parametrize('value', [None, True, -1, 'NaN', 'Infinity', 'invalid'])
def test_unknown_or_invalid_cost_keeps_reservation(tmp_path, value):
    b = budget(tmp_path)
    reservation = attempt(b)
    r = receipt(value, status='failed')
    b.settle(reservation, r)
    assert b.stats()['held_cost_usd'] == '0.200000000'
    assert b.stats()['unknown_cost_calls'] == 1
    assert b.release(reservation) is False
    b.settle(reservation, r | {'actual_cost_usd': '0.04'})
    assert b.stats()['held_calls'] == 0
    assert b.stats()['confirmed_actual_cost_usd'] == '0.040000000'


def test_actual_cost_above_quote_is_recorded_and_stops_further_spend(tmp_path):
    b = budget(tmp_path, max_monthly_cogs='0.25', fixed_monthly_cost=0)
    b.settle(attempt(b), receipt('0.3'))
    assert b.stats()['over_budget'] is True
    assert b.stats()['confirmed_actual_cost_usd'] == '0.300000000'
    with pytest.raises(ActualBudgetExceeded):
        b.reserve('other', quote('0.001'))


def test_nano_usd_rounds_up_and_never_forgives_sub_micro_cost(tmp_path):
    b = budget(tmp_path, max_monthly_cogs='0.000000002', fixed_monthly_cost=0)
    b.settle(attempt(b, value=quote('0.0000000001')), receipt('0.0000000001'))
    assert b.stats()['confirmed_actual_cost_usd'] == '0.000000001'
    b.reserve('second', quote('0.0000000001'))
    with pytest.raises(ActualBudgetExceeded):
        b.reserve('third', quote('0.0000000001'))


def test_concurrent_reservations_cannot_overspend(tmp_path):
    b = budget(tmp_path, max_monthly_cogs='0.3', fixed_monthly_cost=0)
    def reserve(i):
        try:
            b.reserve(f'run-{i}', quote('0.1'))
            return True
        except ActualBudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(reserve, range(12))) == 3
    assert b.stats()['accounted_cost_usd'] == '0.300000000'


def test_only_unstarted_holds_can_be_released(tmp_path):
    b = budget(tmp_path)
    reservation = b.reserve('run', quote())
    assert b.release(reservation) is True
    assert b.release(reservation) is False
    with pytest.raises(ActualBudgetInFlight):
        b.mark_started(reservation)
    replacement = attempt(b)
    assert b.release(replacement) is False


def test_unknown_hold_survives_month_change_but_confirmed_prior_month_does_not(tmp_path):
    clock = [NOW]
    b = ActualBudget(tmp_path / 'actual.sqlite', clock=lambda: clock[0],
                     max_monthly_cogs='0.4', fixed_monthly_cost=0)
    b.settle(attempt(b, 'known'), receipt('0.1'))
    b.settle(attempt(b, 'unknown'), receipt(None, status='failed'))
    clock[0] = datetime(2026, 11, 1, tzinfo=ZoneInfo('Asia/Seoul'))
    assert b.stats()['confirmed_actual_cost_usd'] == '0.000000000'
    assert b.stats()['held_cost_usd'] == '0.200000000'
    with pytest.raises(ActualBudgetExceeded):
        b.reserve('too-much', quote('0.21'))


def test_incremental_langfuse_and_fixed_cost_reserve_budget(tmp_path, monkeypatch):
    monkeypatch.setenv('VIVI_ACTUAL_MAX_MONTHLY_COGS_USD', '20')
    monkeypatch.setenv('VIVI_ACTUAL_FIXED_MONTHLY_COST_USD', '10.5')
    monkeypatch.setenv('VIVI_LANGFUSE_INCREMENTAL_MONTHLY_COST_USD', '9.4')
    b = budget(tmp_path)
    with pytest.raises(ActualBudgetExceeded):
        b.reserve('run', quote('0.101'))
    b.reserve('run', quote('0.1'))
    assert b.stats()['monthly_total_cogs_usd'] == '20.000000000'


def make_legacy(tmp_path, key='old', r=None, completed=True):
    path = tmp_path / 'assignment.sqlite'
    old = Ledger(path, clock=lambda: NOW)
    old_id = old.reserve(key, 'design', 'assignment-prompt-and-schema-hash')
    if completed:
        old.complete(old_id, {}, r or receipt())
    elif r is not None:
        old.fail(old_id, 'ProviderError', r)
    else:
        old.fail(old_id, 'ProviderError')
    return path, old, old_id


def test_legacy_known_success_and_failure_import_once_by_both_identities(tmp_path):
    b = budget(tmp_path)
    r = receipt('0.08', status='failed')
    path, old, old_id = make_legacy(tmp_path, r=r, completed=False)
    duplicate = old.reserve('duplicate-copy', 'design', 'new-hash')
    old.complete(duplicate, {}, r)
    assert b.import_legacy(path) == {'imported': 1, 'matched': 1}
    assert b.import_legacy(path) == {'imported': 0, 'matched': 2}
    assert b.stats()['confirmed_calls_this_month'] == 1
    assert b.stats()['confirmed_actual_cost_usd'] == '0.080000000'


def test_legacy_unknown_blocks_spend_then_backfill_resolves_without_double_count(tmp_path):
    b = budget(tmp_path)
    path, old, old_id = make_legacy(tmp_path, completed=False)
    b.import_legacy(path)
    assert b.stats()['unknown_cost_calls'] == 1
    with pytest.raises(ActualBudgetExceeded):
        b.reserve('new', quote('0.001'))
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE model_calls SET provider_receipt_json=? WHERE id=?',
                     (json.dumps(receipt('0.04', status='failed')), old_id))
    b.import_legacy(path)
    b.import_legacy(path)
    assert b.stats()['unknown_cost_calls'] == 0
    assert b.stats()['confirmed_actual_cost_usd'] == '0.040000000'
    b.reserve('new', quote())


def test_bound_live_assignment_receipt_does_not_duplicate_charge_on_import(tmp_path):
    b = budget(tmp_path)
    old = Ledger(tmp_path / 'assignment.sqlite', clock=lambda: NOW)
    reservation = b.reserve('run', quote())
    old_id = old.reserve('run', 'design', 'schema-hash')
    b.bind_legacy(reservation, old_id)
    b.import_legacy(old.path)
    assert b.stats()['held_cost_usd'] == '0.200000000'
    b.mark_started(reservation)
    r = receipt('0.06')
    old.complete(old_id, {}, r)
    b.import_legacy(old.path)
    b.settle(reservation, r)
    b.import_legacy(old.path)
    assert b.stats()['confirmed_calls_this_month'] == 1
    assert b.stats()['confirmed_actual_cost_usd'] == '0.060000000'


def test_concurrent_legacy_import_before_bind_merges_placeholder(tmp_path):
    b = budget(tmp_path)
    old = Ledger(tmp_path / 'assignment.sqlite', clock=lambda: NOW)
    reservation = b.reserve('run', quote())
    old_id = old.reserve('run', 'design', 'schema-hash')
    b.import_legacy(old.path)
    assert b.stats()['held_calls'] == 2
    b.bind_legacy(reservation, old_id)
    assert b.stats()['held_calls'] == 1
    assert b.stats()['held_cost_usd'] == '0.200000000'
    b.mark_started(reservation)
    b.settle(reservation, receipt('0.05'))
    assert b.stats()['confirmed_actual_cost_usd'] == '0.050000000'


def test_unstarted_legacy_pair_is_not_an_inference_bill(tmp_path):
    b = budget(tmp_path)
    old = Ledger(tmp_path / 'assignment.sqlite', clock=lambda: NOW)
    old.reserve_pair('unstarted-review-pair')
    b.import_legacy(old.path)
    assert b.stats()['held_calls'] == 0


def test_conflicting_receipt_never_releases_a_reservation(tmp_path):
    b = budget(tmp_path)
    reservation = attempt(b)
    with pytest.raises(ActualBudgetInFlight):
        b.settle(reservation, receipt(prompt_hash='another-prompt'))
    assert b.stats()['held_cost_usd'] == '0.200000000'
    b.settle(reservation, receipt('0.01', raw_usage={'cost': '0.02'}))
    assert b.stats()['unknown_cost_calls'] == 1


def test_missing_receipt_and_boolean_call_count_are_unknown(tmp_path):
    b = budget(tmp_path)
    b.settle(attempt(b, 'empty'), {})
    b.settle(attempt(b, 'invalid-count'), receipt(0, actual_provider_calls=True))
    assert b.stats()['unknown_cost_calls'] == 2
    assert b.stats()['held_cost_usd'] == '0.400000000'


def test_isolated_customer_files_have_independent_budgets(tmp_path):
    first = budget(tmp_path / 'first', max_monthly_cogs='0.2', fixed_monthly_cost=0)
    second = budget(tmp_path / 'second', max_monthly_cogs='0.2', fixed_monthly_cost=0)
    first.reserve('same-run', quote())
    second.reserve('same-run', quote())
    assert first.stats()['held_calls'] == second.stats()['held_calls'] == 1
