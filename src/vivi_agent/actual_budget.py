"""고객별 실제 비용을 예약하고 공급자 영수증으로 정산한다.

생성 실패도 확인된 비용으로 정산하며, 미확인 비용은 월이 바뀌어도 예약을 유지한다.
과제 가정 비용과 별도로 Decimal·나노달러 단위를 쓰고 인증 정보나 본문은 저장하지 않는다.
"""
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, localcontext
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
from zoneinfo import ZoneInfo


NANO = Decimal(1_000_000_000)
PRICE_CATALOG_ID = "openai-openrouter-2026-10-15"
# 공식 OpenAI 모델 문서와 docs/cost-model-catalog.json으로 확인한 일반 텍스트 요금이다.
# 유료 도구 비용·캐시 할인·배치 할인은 반영하지 않는다.
PRICES = {
    "gpt-5.4": {"input": "0.0000025", "output": "0.000015", "context": 1050000},
    "gpt-5.4-mini": {"input": "0.00000075", "output": "0.0000045", "context": 400000},
    "gpt-5.4-nano": {"input": "0.0000002", "output": "0.00000125", "context": 400000},
}
MODEL_ALIASES = {"gpt-5.4-2026-03-05": "gpt-5.4"}


class ActualBudgetError(RuntimeError):
    pass


class ActualBudgetExceeded(ActualBudgetError):
    pass


class ActualBudgetInFlight(ActualBudgetError):
    pass


class ActualPriceUnknown(ActualBudgetError):
    pass


def _money(value):
    if isinstance(value, bool) or value is None:
        raise ValueError("A finite nonnegative USD amount is required")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("A finite nonnegative USD amount is required") from None
    if not result.is_finite() or result < 0 or result > Decimal("1000000000"):
        raise ValueError("A finite nonnegative USD amount is required")
    return result


def _nanos(value, ceiling=True):
    amount = _money(value)
    with localcontext() as context:
        context.prec = max(50, len(amount.as_tuple().digits) + 10)
        return int((amount * NANO).to_integral_value(rounding=ROUND_CEILING if ceiling else ROUND_FLOOR))


def _usd(value):
    return format(Decimal(value) / NANO, ".9f")


def _canonical_model(provider, model):
    if not isinstance(model, str):
        raise ActualPriceUnknown("No pinned text price for this provider/model")
    if provider == "openrouter" and isinstance(model, str) and model.startswith("openai/"):
        canonical = model.removeprefix("openai/")
    elif provider == "openai":
        canonical = MODEL_ALIASES.get(model, model)
    else:
        raise ActualPriceUnknown("No pinned text price for this provider/model")
    if canonical not in PRICES:
        raise ActualPriceUnknown("No pinned text price for this provider/model")
    return canonical


def estimate_max_cost(provider, model, input_token_upper_bound, max_output_tokens,
                      *, prompt_hash=None, profile=None, stage="design"):
    canonical = _canonical_model(provider, model)
    if (type(input_token_upper_bound) is not int or type(max_output_tokens) is not int
            or input_token_upper_bound < 1 or not 1 <= max_output_tokens <= 128000):
        raise ActualPriceUnknown("A bounded text token count is required")
    price = PRICES[canonical]
    if input_token_upper_bound + max_output_tokens > price["context"]:
        raise ActualPriceUnknown("Conservative input bound exceeds the priced context window")
    input_rate, output_rate = Decimal(price["input"]), Decimal(price["output"])
    long_context = canonical == "gpt-5.4" and input_token_upper_bound >= 272000
    if long_context:
        input_rate *= 2
        output_rate *= Decimal("1.5")
    amount = Decimal(input_token_upper_bound) * input_rate + Decimal(max_output_tokens) * output_rate
    return {"pricing_known": True, "price_catalog_id": PRICE_CATALOG_ID,
            "currency": "USD", "provider": provider, "requested_model": model,
            "input_token_upper_bound": input_token_upper_bound, "max_output_tokens": max_output_tokens,
            "uncached_input_usd_per_token": str(input_rate), "output_usd_per_token": str(output_rate),
            "long_context_pricing": long_context, "estimated_max_cost_usd": _usd(_nanos(amount)),
            "token_estimate_method": "serialized_text_bytes_plus_4096_framing_allowance",
            "prompt_hash": prompt_hash, "profile": profile or {}, "stage": stage}


def _receipt_actual(receipt):
    if (not isinstance(receipt, dict) or type(receipt.get("actual_provider_calls")) is not int
            or receipt.get("actual_provider_calls") != 1):
        return None
    try:
        value = _money(receipt.get("actual_cost_usd"))
    except ValueError:
        return None
    raw = receipt.get("raw_usage")
    if isinstance(raw, dict) and raw.get("cost") is not None:
        try:
            if _money(raw["cost"]) != value:
                return None
        except ValueError:
            return None
    return _nanos(value)


class ActualBudget:
    def __init__(self, path, max_monthly_cogs=None, fixed_monthly_cost=None,
                 langfuse_incremental_cost=None, clock=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.maximum = _nanos(max_monthly_cogs if max_monthly_cogs is not None else
                              os.environ.get("VIVI_ACTUAL_MAX_MONTHLY_COGS_USD", "20"), ceiling=False)
        self.fixed = _nanos(fixed_monthly_cost if fixed_monthly_cost is not None else
                           os.environ.get("VIVI_ACTUAL_FIXED_MONTHLY_COST_USD", "10.5"))
        self.langfuse = _nanos(langfuse_incremental_cost if langfuse_incremental_cost is not None else
                              os.environ.get("VIVI_LANGFUSE_INCREMENTAL_MONTHLY_COST_USD", "0"))
        if self.maximum <= 0:
            raise ValueError("Actual monthly maximum must be positive")
        self.clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Seoul")))
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS actual_calls (
                    id TEXT PRIMARY KEY, run_key TEXT NOT NULL, month TEXT NOT NULL,
                    created_at TEXT NOT NULL, origin TEXT NOT NULL,
                    status TEXT NOT NULL, started INTEGER NOT NULL DEFAULT 0,
                    reserved_nanos INTEGER NOT NULL, accounted_nanos INTEGER NOT NULL,
                    actual_nanos INTEGER, provider_invocation_id TEXT UNIQUE,
                    quote_json TEXT NOT NULL, receipt_metadata_json TEXT
                );
                CREATE INDEX IF NOT EXISTS actual_calls_run ON actual_calls(run_key);
                CREATE TABLE IF NOT EXISTS legacy_refs (
                    legacy_id TEXT PRIMARY KEY, actual_id TEXT NOT NULL REFERENCES actual_calls(id)
                );
            """)

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _month(self):
        return self.clock().strftime("%Y-%m")

    def _totals(self, conn, month):
        # 이전 달의 미확인 비용도 새 예산에서 계속 잡아 둔다.
        confirmed = conn.execute("SELECT COALESCE(SUM(actual_nanos),0) FROM actual_calls "
                                 "WHERE status='settled' AND month=?", (month,)).fetchone()[0]
        held = conn.execute("SELECT COALESCE(SUM(accounted_nanos),0) FROM actual_calls "
                            "WHERE status IN ('reserved','started','unknown')").fetchone()[0]
        return confirmed, held

    def reserve(self, run_key, quote):
        if (not isinstance(quote, dict) or quote.get("pricing_known") is not True
                or quote.get("price_catalog_id") != PRICE_CATALOG_ID
                or quote.get("currency", "USD") != "USD"):
            raise ActualPriceUnknown("A current pinned USD quote is required")
        _canonical_model(quote.get("provider"), quote.get("requested_model"))
        try:
            reserved = _nanos(quote.get("estimated_max_cost_usd"))
        except ValueError:
            raise ActualPriceUnknown("A finite maximum cost is required") from None
        if reserved <= 0 or not isinstance(run_key, str) or not run_key:
            raise ActualPriceUnknown("A positive priced reservation and run key are required")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM actual_calls WHERE run_key=? AND status!='released'",
                            (run_key,)).fetchone():
                raise ActualBudgetInFlight("This run already has an accounted or uncertain paid attempt")
            actual, held = self._totals(conn, self._month())
            if self.fixed + self.langfuse + actual + held + reserved > self.maximum:
                raise ActualBudgetExceeded("Actual USD reservation exceeds the remaining monthly budget")
            receipt_id = uuid.uuid4().hex
            now = self.clock()
            conn.execute("INSERT INTO actual_calls "
                         "(id,run_key,month,created_at,origin,status,reserved_nanos,accounted_nanos,quote_json) "
                         "VALUES (?,?,?,?,?,'reserved',?,?,?)",
                         (receipt_id, run_key, now.strftime("%Y-%m"), now.isoformat(), "native",
                          reserved, reserved, json.dumps(quote, sort_keys=True)))
            conn.execute("COMMIT")
            return receipt_id

    def bind_legacy(self, reservation_id, legacy_receipt_id):
        if not isinstance(legacy_receipt_id, str) or not legacy_receipt_id:
            raise ValueError("An assignment receipt ID is required")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, reservation_id)
            prior = conn.execute("SELECT actual_id FROM legacy_refs WHERE legacy_id=?", (legacy_receipt_id,)).fetchone()
            if prior and prior[0] != reservation_id:
                imported = self._row(conn, prior[0])
                if (imported["origin"] != "legacy" or imported["actual_nanos"] is not None
                        or imported["run_key"] != row["run_key"]):
                    raise ActualBudgetInFlight("Assignment receipt already belongs to a different attempt")
                # 동시 가져오기가 bind_legacy 전에 예약을 발견했다면
                # 미확인 임시 기록을 실제 견적 예약으로 대체한다.
                conn.execute("UPDATE legacy_refs SET actual_id=? WHERE actual_id=?", (reservation_id, prior[0]))
                conn.execute("DELETE FROM actual_calls WHERE id=?", (prior[0],))
            else:
                conn.execute("INSERT OR IGNORE INTO legacy_refs VALUES (?,?)", (legacy_receipt_id, reservation_id))
            conn.execute("COMMIT")

    def _row(self, conn, reservation_id):
        row = conn.execute("SELECT * FROM actual_calls WHERE id=?", (reservation_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown actual-cost reservation")
        return row

    def mark_started(self, reservation_id):
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._row(conn, reservation_id)
            if row["status"] != "reserved" or row["started"]:
                raise ActualBudgetInFlight("Only an unstarted reservation may begin a provider attempt")
            actual, held = self._totals(conn, self._month())
            if self.fixed + self.langfuse + actual + held > self.maximum:
                raise ActualBudgetExceeded("Actual budget changed before the provider attempt")
            conn.execute("UPDATE actual_calls SET status='started',started=1,month=? WHERE id=?",
                         (self._month(), reservation_id))
            conn.execute("COMMIT")

    def release(self, reservation_id):
        with self._connection() as conn:
            result = conn.execute("UPDATE actual_calls SET status='released',accounted_nanos=0 "
                                  "WHERE id=? AND status='reserved' AND started=0", (reservation_id,))
            return result.rowcount == 1

    def _settle(self, conn, row, receipt, *, imported=False):
        receipt = receipt if isinstance(receipt, dict) else {}
        if row["status"] == "released" or (not imported and not row["started"]):
            raise ActualBudgetInFlight("Only an attempted reservation may be settled")
        quote = json.loads(row["quote_json"])
        amount = _receipt_actual(receipt)
        if not imported:
            for key in ("prompt_hash", "requested_model"):
                if (quote.get(key) is not None and (key in receipt or amount is not None)
                        and receipt.get(key) != quote[key]):
                    raise ActualBudgetInFlight("Provider receipt does not match the reserved request")
        invocation = receipt.get("invocation_id")
        if not isinstance(invocation, str) or not invocation:
            invocation = None
        if row["provider_invocation_id"] and invocation and row["provider_invocation_id"] != invocation:
            raise ActualBudgetInFlight("Provider invocation changed during settlement")
        invocation = invocation or row["provider_invocation_id"]
        if invocation:
            existing = conn.execute("SELECT id FROM actual_calls WHERE provider_invocation_id=?", (invocation,)).fetchone()
            if existing and existing[0] != row["id"]:
                raise ActualBudgetInFlight("Provider invocation is already accounted")
        if row["status"] == "settled":
            if amount != row["actual_nanos"]:
                raise ActualBudgetInFlight("A settled provider cost cannot be overwritten")
            return
        metadata = {key: receipt.get(key) for key in
                    ("invocation_id", "provider", "requested_model", "model", "status", "actual_provider_calls",
                     "actual_cost_usd", "actual_cost_source", "prompt_hash", "started_at", "finished_at")}
        accounted = amount if amount is not None else row["accounted_nanos"]
        conn.execute("UPDATE actual_calls SET status=?,started=1,actual_nanos=?,accounted_nanos=?, "
                     "provider_invocation_id=?,receipt_metadata_json=? WHERE id=?",
                     ("settled" if amount is not None else "unknown", amount, accounted, invocation,
                      json.dumps(metadata, default=str), row["id"]))

    def settle(self, reservation_id, receipt):
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._settle(conn, self._row(conn, reservation_id), receipt)
            conn.execute("COMMIT")
        return self.stats()

    def import_legacy(self, path):
        """기존 과제 원장의 영수증을 중복 없이 가져온다.

        원장 행 UUID와 공급자 호출 ID로 중복을 막는다. 실제 비용을 알 수 없는 호출은
        새 지출을 막고, 나중에 확보한 영수증으로 정산할 수 있다.
        """
        path = Path(path)
        if not path.exists():
            return {"imported": 0, "matched": 0}
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as source:
            source.row_factory = sqlite3.Row
            columns = {row[1] for row in source.execute("PRAGMA table_info(model_calls)")}
            if not columns:
                raise ActualBudgetError("Assignment ledger schema is missing")
            legacy = source.execute("SELECT * FROM model_calls WHERE status!='released'").fetchall()
        imported_count = matched_count = 0
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for old in legacy:
                if "attempt_started" in columns and not old["attempt_started"]:
                    continue
                try:
                    receipt = json.loads(old["provider_receipt_json"] or "{}", parse_float=Decimal)
                except (json.JSONDecodeError, TypeError):
                    receipt = {}
                if not isinstance(receipt, dict):
                    receipt = {}
                invocation = receipt.get("invocation_id")
                if not isinstance(invocation, str) or not invocation:
                    invocation = None
                ref = conn.execute("SELECT actual_id FROM legacy_refs WHERE legacy_id=?", (old["id"],)).fetchone()
                duplicate = conn.execute("SELECT id FROM actual_calls WHERE provider_invocation_id=?", (invocation,)).fetchone() if invocation else None
                if ref or duplicate:
                    actual_id = ref[0] if ref else duplicate[0]
                    row = self._row(conn, actual_id)
                    if ref and duplicate and ref[0] != duplicate[0]:
                        raise ActualBudgetInFlight("Legacy receipt identities disagree")
                    # 연결된 예약이 이미 진행 중인 호출의 예산을 확보하고 있다.
                    # 빈 과거 영수증으로 확인된 견적을 덮어쓰지 않는다.
                    if receipt:
                        self._settle(conn, row, receipt, imported=True)
                    conn.execute("INSERT OR IGNORE INTO legacy_refs VALUES (?,?)", (old["id"], actual_id))
                    matched_count += 1
                    continue
                now = self.clock()
                month = old["month"]
                if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
                    month = now.strftime("%Y-%m")
                # 과거 호출의 가격을 모르면 토큰 상한도 산정할 수 없으므로
                # 실제 영수증을 확보할 때까지 모델 예산 전체를 잡아 둔다.
                hold = max(1, self.maximum - self.fixed - self.langfuse)
                actual_id = uuid.uuid4().hex
                conn.execute("INSERT INTO actual_calls "
                             "(id,run_key,month,created_at,origin,status,started,reserved_nanos,accounted_nanos,quote_json) "
                             "VALUES (?,?,?,?,?,'unknown',1,?,?,?)",
                             (actual_id, old["run_key"], month, old["created_at"], "legacy", hold, hold, "{}"))
                self._settle(conn, self._row(conn, actual_id), receipt, imported=True)
                conn.execute("INSERT INTO legacy_refs VALUES (?,?)", (old["id"], actual_id))
                imported_count += 1
            conn.execute("COMMIT")
        return {"imported": imported_count, "matched": matched_count}

    def stats(self):
        with self._connection() as conn:
            month = self._month()
            actual, held = self._totals(conn, month)
            unresolved = conn.execute("SELECT COUNT(*) FROM actual_calls WHERE status='unknown'").fetchone()[0]
            accounted = actual + held
            cogs = self.fixed + self.langfuse + accounted
            return {"month": month, "currency": "USD", "precision": "nano-USD; reservations/charges round up",
                    "confirmed_actual_cost_usd": _usd(actual), "held_cost_usd": _usd(held),
                    "accounted_cost_usd": _usd(accounted), "fixed_monthly_cost_usd": _usd(self.fixed),
                    "incremental_langfuse_cost_usd": _usd(self.langfuse),
                    "monthly_total_cogs_usd": _usd(cogs), "max_monthly_cogs_usd": _usd(self.maximum),
                    "remaining_budget_usd": _usd(max(0, self.maximum - cogs)),
                    "over_budget": cogs > self.maximum, "unknown_cost_calls": unresolved,
                    "held_calls": conn.execute("SELECT COUNT(*) FROM actual_calls WHERE status IN ('reserved','started','unknown')").fetchone()[0],
                    "confirmed_calls_this_month": conn.execute("SELECT COUNT(*) FROM actual_calls WHERE status='settled' AND month=?", (month,)).fetchone()[0]}
