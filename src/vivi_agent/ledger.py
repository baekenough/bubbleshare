"""SQLite에 호출 예산을 예약한다. 실패하거나 실행 여부가 불확실한 시도도 비용을 잡아 둔다."""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


class BudgetExceeded(RuntimeError):
    pass


class InFlightError(RuntimeError):
    pass


def _micros(value):
    return int(Decimal(str(value)) * 1_000_000)


class Ledger:
    def __init__(self, path, max_attempts=5, monthly_call_cap=500, unit_cost=.018,
                 fixed_monthly_cost=10.5, max_monthly_cogs=20, clock=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self.monthly_call_cap = monthly_call_cap
        self.unit_cost = _micros(unit_cost)
        self.fixed_monthly_cost = _micros(fixed_monthly_cost)
        self.max_monthly_cogs = _micros(max_monthly_cogs)
        self.clock = clock or (lambda: datetime.now(ZoneInfo("Asia/Seoul")))
        if self.unit_cost <= 0 or min(max_attempts, monthly_call_cap) < 1:
            raise ValueError("Call cost and attempt caps must be positive")
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS model_calls (
                    id TEXT PRIMARY KEY, run_key TEXT NOT NULL,
                    stage TEXT NOT NULL, prompt_hash TEXT NOT NULL,
                    month TEXT NOT NULL, created_at TEXT NOT NULL,
                    status TEXT NOT NULL, cost_micros INTEGER NOT NULL,
                    result_json TEXT, provider_receipt_json TEXT, error TEXT
                );
                CREATE INDEX IF NOT EXISTS model_calls_run ON model_calls(run_key);
                CREATE INDEX IF NOT EXISTS model_calls_month ON model_calls(month);
            """)
            conn.execute("BEGIN IMMEDIATE")
            try:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(model_calls)")}
                for name, definition in (
                    ("reservation_group", "TEXT"),
                    ("reservation_slot", "TEXT"),
                    ("attempt_started", "INTEGER NOT NULL DEFAULT 1"),
                ):
                    if name not in columns:
                        conn.execute(f"ALTER TABLE model_calls ADD COLUMN {name} {definition}")
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS model_calls_pair "
                    "ON model_calls(run_key,reservation_group,reservation_slot) "
                    "WHERE reservation_group IS NOT NULL"
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _month(self):
        return self.clock().strftime("%Y-%m")

    def _remaining(self, conn, run_key, month):
        run_calls = conn.execute(
            "SELECT COUNT(*) FROM model_calls WHERE run_key=? AND status!='released'", (run_key,),
        ).fetchone()[0]
        monthly = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_micros),0) FROM model_calls "
            "WHERE month=? AND status!='released'",
            (month,),
        ).fetchone()
        return max(0, min(
            self.max_attempts - run_calls,
            self.monthly_call_cap - monthly[0],
            (self.max_monthly_cogs - self.fixed_monthly_cost - monthly[1]) // self.unit_cost,
        ))

    def remaining(self, run_key):
        with self._connection() as conn:
            return self._remaining(conn, run_key, self._month())

    def reserve(self, run_key, stage, prompt_hash):
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                previous = conn.execute(
                    "SELECT id,status FROM model_calls WHERE run_key=? AND stage=? "
                    "AND prompt_hash=? AND status IN ('reserved','completed') "
                    "ORDER BY CASE status WHEN 'completed' THEN 0 ELSE 1 END LIMIT 1",
                    (run_key, stage, prompt_hash),
                ).fetchone()
                if previous:
                    if previous["status"] == "reserved":
                        raise InFlightError("The same stage is already reserved; reconcile before retrying")
                    conn.execute("COMMIT")
                    return previous["id"]
                now = self.clock()
                month = now.strftime("%Y-%m")
                if self._remaining(conn, run_key, month) < 1:
                    raise BudgetExceeded("Run or monthly paid-call budget exhausted")
                receipt_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO model_calls "
                    "(id,run_key,stage,prompt_hash,month,created_at,status,cost_micros) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (receipt_id, run_key, stage, prompt_hash, month, now.isoformat(),
                     "reserved", self.unit_cost),
                )
                conn.execute("COMMIT")
                return receipt_id
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def get_completed(self, run_key, stage, prompt_hash):
        with self._connection() as conn:
            row = conn.execute(
                "SELECT result_json FROM model_calls WHERE run_key=? AND stage=? "
                "AND prompt_hash=? AND status='completed' ORDER BY created_at DESC LIMIT 1",
                (run_key, stage, prompt_hash),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def cancel_before_provider_call(self, receipt_id, error):
        """호출자가 공급자 실행 전임을 아는 실패 경로에서만 취소한다.

        단일 호출 예약은 보수적으로 시작 상태를 기본값으로 쓴다. 여기서는 실제 호출이 없음을
        기록해 비용 대조 과정에서 미확인 과거 과금으로 오인하지 않게 한다.
        """
        with self._connection() as conn:
            result = conn.execute(
                "UPDATE model_calls SET status='released',attempt_started=0,cost_micros=0,error=? "
                "WHERE id=? AND status='reserved' AND result_json IS NULL "
                "AND provider_receipt_json IS NULL AND reservation_group IS NULL",
                (error, receipt_id),
            )
            return result.rowcount == 1

    def reserve_pair(self, run_key):
        """수정과 필수 재검토 예산을 한 트랜잭션에서 함께 예약한다."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                previous = conn.execute(
                    "SELECT id,reservation_slot,status FROM model_calls "
                    "WHERE run_key=? AND reservation_group='repair'", (run_key,),
                ).fetchall()
                if previous:
                    if len(previous) != 2 or any(row["status"] == "released" for row in previous):
                        raise BudgetExceeded("This repair pair was already closed and cannot be restarted")
                    pair = {row["reservation_slot"]: row["id"] for row in previous}
                    conn.execute("COMMIT")
                    return pair
                now = self.clock()
                month = now.strftime("%Y-%m")
                if self._remaining(conn, run_key, month) < 2:
                    raise BudgetExceeded("Revision requires budget for both revision and re-review")
                pair = {slot: uuid.uuid4().hex for slot in ("revise", "re_review")}
                conn.executemany(
                    "INSERT INTO model_calls "
                    "(id,run_key,stage,prompt_hash,month,created_at,status,cost_micros,"
                    "reservation_group,reservation_slot,attempt_started) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [(receipt_id, run_key, f"__pair_{slot}", "__unbound__", month,
                      now.isoformat(), "reserved", self.unit_cost, "repair", slot, 0)
                     for slot, receipt_id in pair.items()],
                )
                conn.execute("COMMIT")
                return pair
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def bind_reservation(self, receipt_id, stage, prompt_hash):
        """실제 요청을 보내기 직전에 예약된 슬롯을 사용 처리한다."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT * FROM model_calls WHERE id=?", (receipt_id,)).fetchone()
                if row is None or row["status"] != "reserved":
                    raise ValueError("Only an unused reserved slot can be bound")
                if row["attempt_started"]:
                    raise InFlightError("This reservation has already started; reconcile before retrying")
                duplicate = conn.execute(
                    "SELECT id FROM model_calls WHERE run_key=? AND stage=? AND prompt_hash=? "
                    "AND status IN ('reserved','completed') AND id!=?",
                    (row["run_key"], stage, prompt_hash, receipt_id),
                ).fetchone()
                if duplicate:
                    raise InFlightError("The same stage already has another reservation or result")
                month = self._month()
                if row["month"] != month:
                    # 결제 월이 바뀌면 미사용 슬롯을 함께 옮겨
                    # 수정 예산만 남고 재검토 예산이 빠지는 일을 막는다.
                    moving = conn.execute(
                        "SELECT COUNT(*),COALESCE(SUM(cost_micros),0) FROM model_calls "
                        "WHERE run_key=? AND reservation_group=? AND status='reserved' "
                        "AND attempt_started=0 AND month!=?",
                        (row["run_key"], row["reservation_group"], month),
                    ).fetchone()
                    monthly = conn.execute(
                        "SELECT COUNT(*),COALESCE(SUM(cost_micros),0) FROM model_calls "
                        "WHERE month=? AND status!='released'", (month,),
                    ).fetchone()
                    if (monthly[0] + moving[0] > self.monthly_call_cap or
                            self.fixed_monthly_cost + monthly[1] + moving[1] > self.max_monthly_cogs):
                        raise BudgetExceeded("The new billing month cannot fund the held repair pair")
                    conn.execute(
                        "UPDATE model_calls SET month=? WHERE run_key=? AND reservation_group=? "
                        "AND status='reserved' AND attempt_started=0 AND month!=?",
                        (month, row["run_key"], row["reservation_group"], month),
                    )
                conn.execute(
                    "UPDATE model_calls SET stage=?,prompt_hash=?,attempt_started=1,month=? WHERE id=?",
                    (stage, prompt_hash, month, receipt_id),
                )
                conn.execute("COMMIT")
                return receipt_id
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def release(self, receipt_id):
        """사용하지 않은 예약만 해제한다. 시작했거나 실행 여부가 불확실한 시도는 환급하지 않는다."""
        with self._connection() as conn:
            result = conn.execute(
                "UPDATE model_calls SET status='released',cost_micros=0 "
                "WHERE id=? AND status='reserved' AND attempt_started=0", (receipt_id,),
            )
            return result.rowcount == 1

    def complete(self, receipt_id, result, provider_receipt):
        with self._connection() as conn:
            updated = conn.execute(
                "UPDATE model_calls SET status='completed', result_json=?, provider_receipt_json=? "
                "WHERE id=? AND status='reserved' AND attempt_started=1",
                (json.dumps(result, ensure_ascii=False),
                 json.dumps(provider_receipt, ensure_ascii=False), receipt_id),
            )
            if updated.rowcount != 1:
                raise ValueError("Only a reserved attempt can be completed")

    def fail(self, receipt_id, error, provider_receipt=None):
        with self._connection() as conn:
            updated = conn.execute(
                "UPDATE model_calls SET status='failed', error=?, provider_receipt_json=? "
                "WHERE id=? AND status='reserved' AND attempt_started=1",
                (str(error), json.dumps(provider_receipt) if provider_receipt is not None else None, receipt_id),
            )
            if updated.rowcount != 1:
                raise ValueError("Only a reserved attempt can be failed")

    def stats(self, run_key):
        month = self._month()
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT status,cost_micros,attempt_started FROM model_calls WHERE run_key=?", (run_key,),
            ).fetchall()
            monthly = conn.execute(
                "SELECT COUNT(*),COALESCE(SUM(cost_micros),0) FROM model_calls "
                "WHERE month=? AND status!='released'",
                (month,),
            ).fetchone()
            return {
                "run_key": run_key, "month": month,
                "calls": sum(row["status"] != "released" for row in rows),
                "attempted_calls": sum(row["attempt_started"] for row in rows if row["status"] != "released"),
                "released": sum(row["status"] == "released" for row in rows),
                "completed": sum(row["status"] == "completed" for row in rows),
                "failed": sum(row["status"] == "failed" for row in rows),
                "reserved": sum(row["status"] == "reserved" for row in rows),
                "charged_cost_usd": sum(row["cost_micros"] for row in rows) / 1_000_000,
                "monthly_calls": monthly[0],
                "monthly_charged_cost_usd": monthly[1] / 1_000_000,
                "monthly_total_cogs_usd": (self.fixed_monthly_cost + monthly[1]) / 1_000_000,
                "remaining": self._remaining(conn, run_key, month),
            }
