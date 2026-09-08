"""存储层：RunRepository 持久化与租户过滤（v2 口径，替换旧 YunpaiGraph 版本）。"""
import pytest

from yunpai_orchestrator.repository import InMemoryRunRepository, SQLiteRunRepository
from yunpai_orchestrator.state import new_state_v2


def test_sqlite_repository_tenant_filter(tmp_path):
    repository = SQLiteRunRepository(tmp_path / "runs.sqlite")
    for tenant in ("a", "b"):
        state = new_state_v2({"message": "hello"}, tenant_id=tenant)
        state["status"] = "completed"
        repository.save(state)
    assert len(repository.list()) == 2
    assert [s["tenant_id"] for s in repository.list(tenant_id="a")] == ["a"]


def test_sqlite_repository_roundtrip_preserves_gate(tmp_path):
    repository = SQLiteRunRepository(tmp_path / "runs.sqlite")
    state = new_state_v2({"message": "m"})
    state["pending_gate"] = {"type": "engineering", "tool": "run_bom_sop_workflow"}
    state["status"] = "waiting_human"
    repository.save(state)
    restored = repository.get(state["run_id"])
    assert restored["pending_gate"]["type"] == "engineering"
    assert restored["thread_id"] == state["thread_id"]


def test_in_memory_repository_basic():
    repository = InMemoryRunRepository()
    state = new_state_v2({"message": "m"})
    repository.save(state)
    assert repository.get(state["run_id"])["run_id"] == state["run_id"]
