import pytest

from yunpai_orchestrator.graph import YunpaiGraph
from yunpai_orchestrator.models import new_state
from yunpai_orchestrator.repository import SQLiteRunRepository

from test_graph import workflow_request


@pytest.mark.asyncio
async def test_sqlite_repository_recovers_gate_across_graph_instances(tmp_path):
    path = tmp_path / "runs.sqlite"
    first = YunpaiGraph(repository=SQLiteRunRepository(path))
    state = await first.run(new_state(workflow_request()))
    restored = first.repository.get(state["run_id"])

    second = YunpaiGraph(repository=SQLiteRunRepository(path))
    resumed = await second.resume(restored, "approve", actor="data-steward")
    assert resumed["pending_gate"]["type"] == "engineering"
    assert resumed["approvals"][0]["actor"] == "data-steward"
    assert second.repository.get(state["run_id"])["current_step"] == "run_bom_sop_workflow"


def test_sqlite_repository_tenant_filter(tmp_path):
    repository = SQLiteRunRepository(tmp_path / "runs.sqlite")
    for tenant in ("a", "b"):
        state = new_state({"message": "hello"}, tenant_id=tenant)
        state["status"] = "completed"
        repository.save(state)
    assert len(repository.list()) == 2
    assert [state["tenant_id"] for state in repository.list(tenant_id="a")] == ["a"]
