from copy import deepcopy
import json
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from vivi_agent.api import create_app
from vivi_agent.store import ActionStore


def result(result_type="propose"):
    return {
        "result_type": result_type, "topic_id": "lead", "dataset_version": "fixture-v1",
        "scope": {"topic_id": "lead", "snapshot_id": "SNAP-2"},
        "plan": {"decision": "recommend", "actions": [{
            "title": "Review the existing guide", "question_ids": ["Q068"],
            "evidence_ids": ["OBS0406"],
        }]},
        "execution_contracts": [], "validation_errors": [],
    }


class Service:
    def __init__(self, response=None):
        self.response = response or result()
        self.calls = []

    def list_topics(self):
        return {"items": [{"topic_id": "lead", "name": "리드 관리", "coverage": 1.0,
                           "mention_rate": 1 / 3, "recommendation_rate": 1 / 3,
                           "priority_score": 80, "priority_reason": "Observed Q068 gap"}],
                "dataset_version": "fixture-v1"}

    def get_topic(self, topic_id, snapshot_id="SNAP-2"):
        if topic_id != "lead" or snapshot_id != "SNAP-2":
            raise KeyError(topic_id)
        return {"evidence": {"scope": {"topic_id": topic_id, "snapshot_id": snapshot_id}},
                "priority": {"score": 80, "reason": "Observed gap"}}

    def run(self, customer_id, topic_id, snapshot_id="SNAP-2"):
        self.calls.append((customer_id, topic_id, snapshot_id))
        if isinstance(self.response, Exception):
            raise self.response
        return deepcopy(self.response)


def client(tmp_path, service=None, customer="server-customer", store=None, **kwargs):
    return TestClient(create_app(service or Service(), state_dir=tmp_path,
                                customer_id=customer, action_store=store, **kwargs))


def test_http_demonstration_flow_survives_restart_and_keeps_original(tmp_path):
    service = Service()
    api = client(tmp_path, service)
    assert api.get("/api/topics").json()["items"][0]["topic_id"] == "lead"
    assert api.get("/api/topics/lead").json()["evidence"]["scope"]["topic_id"] == "lead"
    assert api.get("/api/actions").json() == {"items": []}
    proposal = api.post("/api/proposals", json={"topic_id": "lead"})
    assert proposal.status_code == 200
    assert service.calls == [("server-customer", "lead", "SNAP-2")]
    saved = api.post("/api/actions", json={"proposal_id": proposal.json()["proposal_id"],
                                          "title": "User reviewed title", "notes": "Owner: sales"})
    assert saved.status_code == 201
    action_id = saved.json()["id"]
    updated = api.patch(f"/api/actions/{action_id}", json={"status": "in_progress", "notes": "Started"})
    assert updated.status_code == 200 and updated.json()["status"] == "in_progress"
    restarted = client(tmp_path)
    restored = restarted.get("/api/actions?topic_id=lead").json()["items"][0]
    assert restored["id"] == action_id and restored["notes"] == "Started"
    assert restored["title"] == "User reviewed title"
    assert restored["original_action"]["title"] == "Review the existing guide"
    assert restored["original_proposal"] == result()
    assert restored["observed_outcome"] is None


@pytest.mark.parametrize("result_type", ["no_action", "insufficient_evidence", "fallback"])
def test_nonproposal_agent_branches_are_returned_without_savable_id(tmp_path, result_type):
    api = client(tmp_path, Service({**result(result_type), "plan": None}))
    response = api.post("/api/proposals", json={"topic_id": "lead"})
    assert response.status_code == 200
    assert response.json()["result_type"] == result_type and response.json()["proposal_id"] is None
    assert api.get("/api/actions").json() == {"items": []}


def test_saving_a_cached_proposal_rechecks_current_active_actions(tmp_path):
    service = Service()
    api = client(tmp_path, service)
    first = api.post("/api/proposals", json={"topic_id": "lead"}).json()
    original = api.post("/api/actions", json={"proposal_id": first["proposal_id"]}).json()
    service.response = result("cache_hit")
    cached = api.post("/api/proposals", json={"topic_id": "lead"}).json()
    response = api.post("/api/actions", json={"proposal_id": cached["proposal_id"]})
    assert response.status_code == 409
    assert response.json()["detail"]["existing_action_id"] == original["id"]
    assert len(api.get("/api/actions").json()["items"]) == 1


@pytest.mark.parametrize("client_request", [
    ("get", "/api/actions?customer_id=another", None, {}),
    ("get", "/api/topics?tenant_id=another", None, {}),
    ("get", "/api/actions", None, {"X-Customer-ID": "another"}),
    ("get", "/api/actions", None, {"X-Tenant-ID": "another"}),
    ("post", "/api/proposals", {"topic_id": "lead", "customer_id": "another"}, {}),
    ("post", "/api/actions", {"proposal_id": "unused", "tenant_id": "another"}, {}),
])
def test_client_cannot_choose_customer_scope(tmp_path, client_request):
    api = client(tmp_path)
    method, path, body, headers = client_request
    response = api.request(method, path, json=body, headers=headers)
    assert response.status_code == 422


def test_server_tenants_cannot_save_or_edit_each_others_objects(tmp_path):
    store = ActionStore(tmp_path / "shared.sqlite")
    a, b = client(tmp_path, customer="a", store=store), client(tmp_path, customer="b", store=store)
    proposal = a.post("/api/proposals", json={"topic_id": "lead"}).json()
    action = a.post("/api/actions", json={"proposal_id": proposal["proposal_id"]}).json()
    assert b.get("/api/actions").json() == {"items": []}
    assert b.post("/api/actions", json={"proposal_id": proposal["proposal_id"]}).status_code == 404
    assert b.patch(f"/api/actions/{action['id']}", json={"status": "done"}).status_code == 404
    assert a.get("/api/actions").json()["items"][0]["status"] == "draft"


def test_unknown_identifiers_and_invalid_inputs_have_explicit_statuses(tmp_path):
    api = client(tmp_path)
    assert api.get("/api/topics/missing").status_code == 404
    assert api.post("/api/proposals", json={"topic_id": "missing"}).status_code == 404
    assert api.post("/api/proposals", json={"topic_id": " "}).status_code == 422
    assert api.post("/api/actions", json={"proposal_id": "missing"}).status_code == 404
    assert api.patch("/api/actions/missing", json={"status": "done"}).status_code == 404
    for invalid in [{}, {"status": "successful"}, {"status": None}, {"title": " "}, {"notes": None}]:
        assert api.patch("/api/actions/missing", json=invalid).status_code == 422


def test_runtime_failure_returns_safe_retryable_error(tmp_path):
    api = client(tmp_path, Service(RuntimeError("PRIVATE_ERROR_SENTINEL")))
    response = api.post("/api/proposals", json={"topic_id": "lead"})
    assert response.status_code == 503
    assert "PRIVATE_ERROR_SENTINEL" not in response.text
    assert "retry" in response.json()["detail"]


def test_invalid_generated_plan_is_not_persisted_as_a_proposal(tmp_path):
    invalid = result()
    invalid["validation_errors"] = ["Unknown evidence"]
    api = client(tmp_path, Service(invalid))
    response = api.post("/api/proposals", json={"topic_id": "lead"})
    assert response.status_code == 503
    assert "nothing was saved" in response.json()["detail"]


def test_default_dependency_boundary_uses_fresh_provider_only_for_generation(tmp_path, monkeypatch):
    providers, constructed = [], []

    class Product(Service):
        def __init__(self, repo, provider, state_dir, action_store):
            super().__init__()
            constructed.append((repo, provider, state_dir, action_store))

    def factory():
        provider = object()
        providers.append(provider)
        return provider

    monkeypatch.setitem(sys.modules, "vivi_agent.product", SimpleNamespace(ProductAgent=Product))
    repository = object()
    api = TestClient(create_app(repository=repository, provider_factory=factory,
                               state_dir=tmp_path, customer_id="a"))
    assert api.get("/api/topics").status_code == 200
    assert not providers and constructed[-1][1] is None
    for _ in range(2):
        assert api.post("/api/proposals", json={"topic_id": "lead"}).status_code == 200
    assert len(providers) == 2 and providers[0] is not providers[1]
    assert all(instance[0] is repository for instance in constructed)


def test_absent_provider_key_reaches_explicit_fallback_without_blocking_startup(tmp_path, monkeypatch):
    seen = []

    class Product(Service):
        def __init__(self, repo, provider, state_dir, action_store):
            super().__init__({**result("fallback"), "plan": None, "reason": "No provider key"})
            seen.append(provider)

    monkeypatch.setitem(sys.modules, "vivi_agent.product", SimpleNamespace(ProductAgent=Product))
    monkeypatch.setattr("vivi_agent.provider.provider_api_key_name", lambda: "VIVI_TEST_ABSENT_KEY")
    monkeypatch.delenv("VIVI_TEST_ABSENT_KEY", raising=False)
    api = TestClient(create_app(repository=object(), state_dir=tmp_path, customer_id="a"))
    response = api.post("/api/proposals", json={"topic_id": "lead"})
    assert response.status_code == 200 and response.json()["result_type"] == "fallback"
    assert seen == [None]


def test_default_provider_trace_directory_uses_server_customer_scope(tmp_path, monkeypatch):
    from vivi_agent.graph import digest
    trace_paths = []

    class Product(Service):
        def __init__(self, repo, provider, state_dir, action_store):
            super().__init__()

    monkeypatch.setitem(sys.modules, "vivi_agent.product", SimpleNamespace(ProductAgent=Product))
    monkeypatch.setattr("vivi_agent.provider.provider_api_key_name", lambda: "VIVI_TEST_FAKE_KEY")
    monkeypatch.setenv("VIVI_TEST_FAKE_KEY", "not-a-provider-key")
    monkeypatch.setattr("vivi_agent.provider.create_provider",
                        lambda **kwargs: trace_paths.append(kwargs["artifact_dir"]) or object())
    api = TestClient(create_app(repository=object(), state_dir=tmp_path, customer_id="server-a"))
    assert api.post("/api/proposals", json={"topic_id": "lead"}).status_code == 200
    assert trace_paths == [tmp_path / "customers" / digest("server-a") / "traces"]


def test_spa_fallback_serves_only_files_within_frontend_directory(tmp_path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<html>VIVI fixture</html>")
    (frontend / "bundle.js").write_text("window.fixture=true;")
    secret = tmp_path / "private.txt"
    secret.write_text("PRIVATE_FILE_SENTINEL")
    (frontend / "leak.txt").symlink_to(secret)
    api = client(tmp_path, frontend_dir=frontend)
    assert api.get("/").status_code == 200
    assert "VIVI fixture" in api.get("/topics/lead").text
    assert api.get("/bundle.js").text == "window.fixture=true;"
    for path in ["/api/missing", "/missing.js", "/leak.txt", "/%2e%2e/private.txt"]:
        response = api.get(path)
        assert response.status_code == 404
        assert "PRIVATE_FILE_SENTINEL" not in response.text
