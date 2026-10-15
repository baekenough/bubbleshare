from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from threading import Barrier

import pytest

from vivi_agent.store import ActionStore, DuplicateAction, InvalidStoreInput, StoreNotFound


def proposal_result(*, topic="lead", questions=None, result_type="propose"):
    return {
        "result_type": result_type, "scope": {"topic_id": topic, "snapshot_id": "SNAP-2"},
        "topic_id": topic, "dataset_version": "test-dataset-v1", "validation_errors": [],
        "plan": {"decision": "recommend", "actions": [{
            "title": "Inspect the Q068 guide", "question_ids": questions or ["Q068"],
            "evidence_ids": ["OBS0406"], "steps": [{"instruction": "Inspect the source body."}],
        }]},
        "execution_contracts": [{"scope": "fixed-pairs", "observed_effect": None}],
    }


def test_restart_preserves_user_edits_and_immutable_original(tmp_path):
    path = tmp_path / "actions.sqlite"
    store = ActionStore(path)
    supplied = proposal_result()
    original = deepcopy(supplied)
    proposal = store.save_proposal("customer-a", supplied)
    supplied["plan"]["actions"][0]["title"] = "Caller mutation must not change the proposal"
    action = store.save_action("customer-a", proposal["id"], title="My reviewed title", notes="Owner: sales")
    store.update_action("customer-a", action["id"], status="in_progress", notes="Started")
    restarted = ActionStore(path)
    restored = restarted.list_actions("customer-a")[0]
    assert restored["status"] == "in_progress" and restored["notes"] == "Started"
    assert restored["title"] == "My reviewed title"
    assert restored["original_proposal"] == original
    assert restored["original_action"] == original["plan"]["actions"][0]
    assert restored["plan"] == original["plan"]
    assert restarted.get_proposal("customer-a", proposal["id"])["result"] == original


def test_every_read_and_mutation_is_scoped_to_customer(tmp_path):
    store = ActionStore(tmp_path / "actions.sqlite")
    a_proposal = store.save_proposal("a", proposal_result())
    a_action = store.save_action("a", a_proposal["id"])
    assert store.list_actions("b") == []
    for operation in [
        lambda: store.get_proposal("b", a_proposal["id"]),
        lambda: store.save_action("b", a_proposal["id"]),
        lambda: store.get_action("b", a_action["id"]),
        lambda: store.update_action("b", a_action["id"], status="done"),
    ]:
        with pytest.raises(StoreNotFound):
            operation()
    b_proposal = store.save_proposal("b", proposal_result())
    b_action = store.save_action("b", b_proposal["id"])
    assert b_action["id"] != a_action["id"]
    assert len(store.list_actions("a")) == len(store.list_actions("b")) == 1
    assert store.get_action("a", a_action["id"])["status"] == "draft"


def test_concurrent_saves_cannot_duplicate_active_question_scope(tmp_path):
    store = ActionStore(tmp_path / "actions.sqlite")
    first = store.save_proposal("a", proposal_result(questions=["Q067", "Q068"]))
    second = store.save_proposal("a", proposal_result(questions=["Q068", "Q067"], result_type="cache_hit"))
    barrier = Barrier(2)

    def save(proposal):
        barrier.wait()
        try:
            return store.save_action("a", proposal["id"])["id"]
        except DuplicateAction as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(save, [first, second]))
    assert sum(isinstance(result, DuplicateAction) for result in outcomes) == 1
    saved = store.list_actions("a")
    assert len(saved) == 1
    assert next(result for result in outcomes if isinstance(result, DuplicateAction)).action_id == saved[0]["id"]


def test_cached_proposal_save_rechecks_live_status_and_reopen_is_transactional(tmp_path):
    store = ActionStore(tmp_path / "actions.sqlite")
    first = store.save_proposal("a", proposal_result())
    cached = store.save_proposal("a", proposal_result(result_type="cache_hit"))
    original = store.save_action("a", first["id"])
    with pytest.raises(DuplicateAction):
        store.save_action("a", cached["id"])
    store.update_action("a", original["id"], status="done")
    replacement = store.save_action("a", cached["id"])
    with pytest.raises(DuplicateAction):
        store.update_action("a", original["id"], status="in_progress", notes="Must roll back")
    unchanged = store.get_action("a", original["id"])
    assert unchanged["status"] == "done" and unchanged["notes"] == ""
    assert [action["id"] for action in store.list_actions("a", active_only=True)] == [replacement["id"]]
    store.update_action("a", replacement["id"], status="dismissed")
    reopened = store.update_action("a", original["id"], status="in_progress")
    assert reopened["status"] == "in_progress"


def test_status_and_fields_are_validated_without_partial_edits(tmp_path):
    store = ActionStore(tmp_path / "actions.sqlite")
    proposal = store.save_proposal("a", proposal_result())
    action = store.save_action("a", proposal["id"])
    for kwargs in [{"status": "successful"}, {"title": "  "}, {"notes": "x" * 10001}]:
        with pytest.raises(InvalidStoreInput):
            store.update_action("a", action["id"], **kwargs)
    assert store.get_action("a", action["id"])["status"] == "draft"
    assert store.get_action("a", action["id"])["title"] == action["title"]


@pytest.mark.parametrize("result_type", ["no_action", "insufficient_evidence", "fallback"])
def test_nonproposal_results_cannot_be_saved_even_with_plan(tmp_path, result_type):
    store = ActionStore(tmp_path / "actions.sqlite")
    with pytest.raises(InvalidStoreInput):
        store.save_proposal("a", proposal_result(result_type=result_type))


def test_invalid_proposal_references_and_validation_failures_are_rejected(tmp_path):
    store = ActionStore(tmp_path / "actions.sqlite")
    for questions in [["Q068", "Q068"], [""], "Q068"]:
        with pytest.raises(InvalidStoreInput):
            store.save_proposal("a", proposal_result(questions=questions))
    invalid = proposal_result()
    invalid["validation_errors"] = ["Wrong evidence scope"]
    with pytest.raises(InvalidStoreInput):
        store.save_proposal("a", invalid)
    with pytest.raises(StoreNotFound):
        store.save_action("a", "does-not-exist")
    with pytest.raises(StoreNotFound):
        store.update_action("a", "does-not-exist", status="done")


def test_events_are_append_only_and_status_is_not_observed_effect(tmp_path):
    store = ActionStore(tmp_path / "actions.sqlite")
    customer = "../../tenant-a"
    proposal = store.save_proposal(customer, proposal_result())
    action = store.save_action(customer, proposal["id"], notes="User adopted this draft")
    path = store.event_path(customer)
    before = path.read_bytes()
    store.update_action(customer, action["id"], status="done")
    store.update_action(customer, action["id"], status="dismissed")
    assert path.parent == store.events_dir
    assert path.read_bytes().startswith(before)
    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [event["event_type"] for event in events] == [
        "proposal.created", "action.saved", "action.updated", "action.updated",
    ]
    assert all(event["observed_outcome"] is None and event["outcome_status"] == "unobserved"
               and event["causal_effect_established"] is False for event in events)
    assert events[0]["payload"]["original_proposal"] == proposal_result()
    assert events[-1]["payload"]["status"] == "dismissed"
    assert store.get_action(customer, action["id"])["observed_outcome"] is None


def test_jsonl_failure_does_not_lose_committed_action_and_outbox_retries(tmp_path, monkeypatch):
    path = tmp_path / "actions.sqlite"
    store = ActionStore(path)
    original_flush = store.flush_events

    def unavailable(_customer):
        raise OSError("test filesystem unavailable")

    monkeypatch.setattr(store, "flush_events", unavailable)
    proposal = store.save_proposal("a", proposal_result())
    action = store.save_action("a", proposal["id"])
    assert ActionStore(path).get_action("a", action["id"])["status"] == "draft"
    monkeypatch.setattr(store, "flush_events", original_flush)
    store.flush_events("a")
    first = store.event_path("a").read_bytes()
    store.flush_events("a")
    assert store.event_path("a").read_bytes() == first
    assert len(first.splitlines()) == 2


def test_topic_filter_does_not_include_other_topics(tmp_path):
    store = ActionStore(tmp_path / "actions.sqlite")
    for topic in ["lead", "security"]:
        proposal = store.save_proposal("a", proposal_result(topic=topic))
        store.save_action("a", proposal["id"])
    assert len(store.list_actions("a")) == 2
    assert [row["topic_id"] for row in store.list_actions("a", "lead")] == ["lead"]
