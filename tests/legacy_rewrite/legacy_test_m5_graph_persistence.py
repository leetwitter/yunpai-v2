from fastapi.testclient import TestClient

from yunpai_orchestrator.api import create_app
from yunpai_orchestrator.m5_repository import M5Repository
from yunpai_orchestrator.repository import SQLiteRunRepository

from test_m5_lifecycle_tools import _payload


def test_apply_gate_persists_m5_release_and_head(tmp_path, monkeypatch):
    m5_db = tmp_path / "m5.sqlite"
    monkeypatch.setenv("YUNPAI_M5_DB", str(m5_db))
    payload = _payload()
    payload.update({
        "idempotency_key": "GRAPH-APPLY-1",
        "scenario_id": "SC-GRAPH-APPLY",
        "orders": [{
            **payload["orders"][0],
            "order_id": "SO-GRAPH-APPLY-1",
        }],
    })
    client = TestClient(create_app(repository=SQLiteRunRepository(tmp_path / "runs.sqlite")))
    created = client.post("/runs", json={
        "tenant_id": "tenant-graph-apply",
        "request": {
            "tool": "solve_scheduling",
            "message": "PMC apply persistence regression",
            "payloads": {"solve_scheduling": payload},
        },
    })
    assert created.status_code == 200
    state = created.json()
    assert state["pending_gate"]["type"] == "apply"

    resumed = client.post(
        f"/runs/{state['run_id']}/resume",
        json={"decision": "approve", "actor": "zhb"},
    )
    assert resumed.status_code == 200
    final = resumed.json()
    assert final["status"] == "completed"
    result = final["outputs"]["solve_scheduling"]["data"]
    plan_version = result["schedule"]["plan_version"]

    repo = M5Repository(m5_db)
    plan = repo.get_plan(plan_version)
    assert plan is not None
    assert plan["lifecycle_status"] == "released"
    head = repo.get_head("SC-GRAPH-APPLY")
    assert head["head_plan_version"] == plan_version
    assert [event["to_status"] for event in repo.lifecycle_events(plan_version)] == ["approved", "released"]
