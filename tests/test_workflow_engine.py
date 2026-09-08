"""书二 §4.3：DAG 工作流引擎——depends_on 真实调度（治痛点 10）。"""
import pytest

from yunpai_orchestrator.orchestrator.workflow_engine import WorkflowEngine
from yunpai_orchestrator.workflow_registry import KNOWN_WORKFLOWS


@pytest.fixture(scope="module")
def engine():
    return WorkflowEngine()


def test_expand_two_workflows_topo(engine):
    for wid in KNOWN_WORKFLOWS:
        plan = engine.expand(wid)
        assert plan, f"{wid} 展开为空"
        done: set[str] = set()
        for step in plan:  # 拓扑序：每步依赖都已在其之前完成
            assert set(step["depends_on"]) <= done
            done.add(step["step_id"])


def test_m1_m5_chain_structure(engine):
    plan = engine.expand("m1_m5_document_to_plan")
    ids = [s["step_id"] for s in plan]
    assert ids == ["m1_parse", "m0_ingest", "m0_publish", "m2_engineering",
                   "m3_mrp", "m4_procurement", "m5_snapshot", "m5_solve", "m5_readback"]
    assert plan[-1]["tool"] == "get_m5_schedule"


def test_ready_respects_dependencies(engine):
    plan = engine.expand("m1_m5_document_to_plan")
    assert engine.ready(plan) == ["m1_parse"]
    marked = engine.mark(plan, "m1_parse", "completed")
    assert engine.ready(marked) == ["m0_ingest"]
    # 上游未完成时下游不 ready
    pending_only = [dict(s, status="pending") for s in plan]
    assert "m5_solve" not in engine.ready(pending_only)


def test_mark_and_stats(engine):
    plan = engine.mark(engine.expand("canonical_to_m5"), "canonical_verify", "completed")
    stats = engine.stats(plan)
    assert stats == {"completed": 1, "pending": 6}


def test_unknown_workflow_raises(engine):
    with pytest.raises(KeyError):
        engine.expand("m0_m5")  # 旧版已删除的工作流不得复活
