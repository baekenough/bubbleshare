"""고객별 제안과 사용자 액션을 저장한다. SQLite를 기준 저장소로 쓴다."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid


STATUSES = {"draft", "in_progress", "done", "dismissed"}
ACTIVE_STATUSES = {"draft", "in_progress"}


class StoreNotFound(LookupError):
    pass


class InvalidStoreInput(ValueError):
    pass


class DuplicateAction(ValueError):
    def __init__(self, action_id):
        self.action_id = action_id
        super().__init__("An active action already covers these questions in this topic.")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value, field, *, maximum=300, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise InvalidStoreInput(f"{field} must be {'a' if empty else 'a nonempty'} string of at most {maximum} characters")
    return value if empty else value.strip()


class ActionStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.events_dir = self.path.parent / "events"
        self.events_dir.mkdir(exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
                    topic_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                    question_ids_json TEXT NOT NULL, result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(customer_id,id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
                    proposal_id TEXT NOT NULL, topic_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL, question_ids_json TEXT NOT NULL,
                    title TEXT NOT NULL, notes TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('draft','in_progress','done','dismissed')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(customer_id,proposal_id) REFERENCES proposals(customer_id,id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS active_action_scope
                    ON actions(customer_id,topic_id,question_ids_json)
                    WHERE status IN ('draft','in_progress');
                CREATE INDEX IF NOT EXISTS customer_actions ON actions(customer_id,created_at);
                CREATE TABLE IF NOT EXISTS action_events (
                    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
                    event_json TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def _event(self, db, customer_id, event_type, payload):
        event = {
            "id": "evt_" + uuid.uuid4().hex,
            "customer_id": customer_id,
            "event_type": event_type,
            "created_at": _now(),
            "payload": payload,
            "observed_outcome": None,
            "outcome_status": "unobserved",
            "causal_effect_established": False,
        }
        db.execute(
            "INSERT INTO action_events(id,customer_id,event_json,created_at) VALUES(?,?,?,?)",
            (event["id"], customer_id, _json(event), event["created_at"]),
        )
        return event

    def event_path(self, customer_id):
        """고객 식별자로 임의 파일 경로를 지정하지 못하게 한다."""
        return self.events_dir / (hashlib.sha256(customer_id.encode()).hexdigest() + ".jsonl")

    def flush_events(self, customer_id):
        """트랜잭션 아웃박스를 기존 JSONL 기록에 이어 쓴다.

        이벤트 ID로 중단된 쓰기를 중복 없이 복구한다. 파일 쓰기가 실패해도 SQLite 이벤트는
        남아 있으며 다음 호출에서 다시 시도한다.
        """
        customer_id = _text(customer_id, "customer_id")
        path = self.event_path(customer_id)
        with path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                stream.seek(0)
                seen = set()
                for line in stream:
                    try:
                        seen.add(json.loads(line)["id"])
                    except (ValueError, KeyError):
                        # 중단된 마지막 줄은 보존한다. 전체 이벤트는 SQLite에 남아 있다.
                        continue
                with self._connection() as db:
                    rows = db.execute(
                        "SELECT id,event_json FROM action_events WHERE customer_id=? AND delivered=0 "
                        "ORDER BY created_at,id", (customer_id,),
                    ).fetchall()
                    stream.seek(0, 2)
                    for row in rows:
                        if row["id"] not in seen:
                            stream.write(row["event_json"] + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                    for row in rows:
                        db.execute("UPDATE action_events SET delivered=1 WHERE customer_id=? AND id=?",
                                   (customer_id, row["id"]))
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def _export_events(self, customer_id):
        try:
            self.flush_events(customer_id)
        except OSError:
            # 사용자 상태는 이미 저장됐다. 다음 내보내기에서 아웃박스를 다시 처리한다.
            pass

    def record_event(self, customer_id, event_type, payload):
        customer_id = _text(customer_id, "customer_id")
        event_type = _text(event_type, "event_type")
        with self._transaction() as db:
            event = self._event(db, customer_id, event_type, payload)
        self._export_events(customer_id)
        return event

    @staticmethod
    def _proposal(row):
        return {
            "id": row["id"], "customer_id": row["customer_id"],
            "topic_id": row["topic_id"], "snapshot_id": row["snapshot_id"],
            "question_ids": json.loads(row["question_ids_json"]),
            "result": json.loads(row["result_json"]), "created_at": row["created_at"],
        }

    def save_proposal(self, customer_id, result):
        customer_id = _text(customer_id, "customer_id")
        if not isinstance(result, dict) or result.get("result_type") not in {"propose", "cache_hit"}:
            raise InvalidStoreInput("Only a validated propose or cache_hit result can be saved.")
        plan = result.get("plan")
        if not isinstance(plan, dict):
            raise InvalidStoreInput("A proposal must contain a validated plan.")
        actions = plan.get("actions")
        if (plan.get("decision") != "recommend" or not isinstance(actions, list) or len(actions) != 1
                or not isinstance(actions[0], dict)
                or result.get("validation_errors") or result.get("review_errors")):
            raise InvalidStoreInput("A proposal must contain exactly one validated recommendation.")
        action = actions[0]
        _text(action.get("title"), "action.title")
        question_ids = action.get("question_ids")
        if (not isinstance(question_ids, list) or not question_ids
                or any(not isinstance(q, str) or not q.strip() for q in question_ids)
                or len(set(question_ids)) != len(question_ids)):
            raise InvalidStoreInput("A proposal requires distinct nonempty question identifiers.")
        scope = result.get("scope") or {}
        if not isinstance(scope, dict):
            raise InvalidStoreInput("A proposal requires a valid scope.")
        topic = _text(scope.get("topic_id", result.get("topic_id")), "topic_id")
        snapshot = _text(scope.get("snapshot_id", "SNAP-2"), "snapshot_id")
        proposal_id, created = "prp_" + uuid.uuid4().hex, _now()
        with self._transaction() as db:
            db.execute("INSERT INTO proposals VALUES(?,?,?,?,?,?,?)", (
                proposal_id, customer_id, topic, snapshot, _json(sorted(question_ids)), _json(result), created,
            ))
            self._event(db, customer_id, "proposal.created", {
                "proposal_id": proposal_id, "original_proposal": result, "adopted": False,
            })
        self._export_events(customer_id)
        return self.get_proposal(customer_id, proposal_id)

    def get_proposal(self, customer_id, proposal_id):
        with self._connection() as db:
            row = db.execute("SELECT * FROM proposals WHERE customer_id=? AND id=?",
                             (customer_id, proposal_id)).fetchone()
        if row is None:
            raise StoreNotFound("Proposal not found.")
        return self._proposal(row)

    @staticmethod
    def _action(row):
        original = json.loads(row["result_json"])
        return {
            "id": row["id"], "customer_id": row["customer_id"], "proposal_id": row["proposal_id"],
            "topic_id": row["topic_id"], "snapshot_id": row["snapshot_id"],
            "question_ids": json.loads(row["question_ids_json"]),
            "title": row["title"], "notes": row["notes"], "status": row["status"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "dataset_version": original.get("dataset_version"),
            "plan": original["plan"], "execution_contracts": original.get("execution_contracts", []),
            "original_action": original["plan"]["actions"][0], "original_proposal": original,
            "observed_outcome": None, "outcome_status": "unobserved",
        }

    _ACTION_SELECT = (
        "SELECT a.*,p.result_json FROM actions a JOIN proposals p "
        "ON p.id=a.proposal_id AND p.customer_id=a.customer_id WHERE a.customer_id=?"
    )

    def get_action(self, customer_id, action_id):
        with self._connection() as db:
            row = db.execute(self._ACTION_SELECT + " AND a.id=?", (customer_id, action_id)).fetchone()
        if row is None:
            raise StoreNotFound("Action not found.")
        return self._action(row)

    def list_actions(self, customer_id, topic_id=None, active_only=False):
        query, values = self._ACTION_SELECT, [customer_id]
        if topic_id is not None:
            query += " AND a.topic_id=?"
            values.append(topic_id)
        if active_only:
            query += " AND a.status IN ('draft','in_progress')"
        with self._connection() as db:
            rows = db.execute(query + " ORDER BY a.created_at DESC,a.id", values).fetchall()
        return [self._action(row) for row in rows]

    @staticmethod
    def _duplicate(db, customer_id, topic_id, question_ids_json, except_id=""):
        return db.execute(
            "SELECT id FROM actions WHERE customer_id=? AND topic_id=? AND question_ids_json=? "
            "AND status IN ('draft','in_progress') AND id!=?",
            (customer_id, topic_id, question_ids_json, except_id),
        ).fetchone()

    def save_action(self, customer_id, proposal_id, title=None, notes=""):
        customer_id = _text(customer_id, "customer_id")
        notes = _text(notes, "notes", maximum=10000, empty=True)
        action_id, now = "act_" + uuid.uuid4().hex, _now()
        with self._transaction() as db:
            proposal = db.execute("SELECT * FROM proposals WHERE customer_id=? AND id=?",
                                  (customer_id, proposal_id)).fetchone()
            if proposal is None:
                raise StoreNotFound("Proposal not found.")
            original = json.loads(proposal["result_json"])
            title = _text(title if title is not None else original["plan"]["actions"][0]["title"], "title")
            duplicate = self._duplicate(db, customer_id, proposal["topic_id"], proposal["question_ids_json"])
            if duplicate:
                raise DuplicateAction(duplicate["id"])
            db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                action_id, customer_id, proposal_id, proposal["topic_id"], proposal["snapshot_id"],
                proposal["question_ids_json"], title, notes, "draft", now, now,
            ))
            self._event(db, customer_id, "action.saved", {
                "action_id": action_id, "proposal_id": proposal_id, "adopted": True,
                "user_edits": {"title": title, "notes": notes}, "status": "draft",
            })
        self._export_events(customer_id)
        return self.get_action(customer_id, action_id)

    def update_action(self, customer_id, action_id, *, status=None, title=None, notes=None):
        if status is not None and status not in STATUSES:
            raise InvalidStoreInput("Unknown action status.")
        if title is not None:
            title = _text(title, "title")
        if notes is not None:
            notes = _text(notes, "notes", maximum=10000, empty=True)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM actions WHERE customer_id=? AND id=?",
                             (customer_id, action_id)).fetchone()
            if row is None:
                raise StoreNotFound("Action not found.")
            changes = {key: value for key, value in {"status": status, "title": title, "notes": notes}.items()
                       if value is not None and value != row[key]}
            next_status = changes.get("status", row["status"])
            if next_status in ACTIVE_STATUSES:
                duplicate = self._duplicate(db, customer_id, row["topic_id"], row["question_ids_json"], action_id)
                if duplicate:
                    raise DuplicateAction(duplicate["id"])
            if changes:
                assignments = ",".join(f"{key}=?" for key in changes)
                db.execute(f"UPDATE actions SET {assignments},updated_at=? WHERE customer_id=? AND id=?",
                           (*changes.values(), _now(), customer_id, action_id))
                self._event(db, customer_id, "action.updated", {
                    "action_id": action_id, "proposal_id": row["proposal_id"], "user_edits": changes,
                    "previous_status": row["status"], "status": next_status,
                })
        self._export_events(customer_id)
        return self.get_action(customer_id, action_id)
