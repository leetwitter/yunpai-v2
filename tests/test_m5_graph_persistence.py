"""apply Gate 真实发布落库（书二 §6.2「apply 释放走 M5 head CAS〔迁〕」）。

rows-S6 需补测试：INT 的 `test_m5_graph_persistence` 迁成 V2 版——
V2 用 LangGraph `interrupt()`/`Command(resume=...)`，并断言 M5 repository 里
真的发生了 draft→approved→released 与 scenario head CAS 推进，
而不是只把 RunState JSON 标成 released。
"""
import asyncio

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from test_graph_smoke import _deps
from test_m5_lifecycle_tools import _payload

from yunpai_orchestrator.graph import build_graph
from yunpai_orchestrator.m5_repository import M5Repository
from yunpai_orchestrator.repository import InMemoryRunRepository
from yunpai_orchestrator.state import new_state_v2


def _free_solve_state(payload, **extra):
    request = {"message": "排程并发布", "tools": ["solve_scheduling"],
               "solve_scheduling": payload, **extra}
    return new_state_v2(request)


def _config(state, limit=64):
    return {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": limit}


def _no_bridge(monkeypatch):
    """本用例只验证「apply 门 → 真实发布」这一段。

    桥接的 solve_scheduling 分支要求 ingest 回读成功（另一条链路的契约），
    这里让桥返回空、改由显式参数装配（装配器 EXPLICIT_ONLY 语义）。
    """
    monkeypatch.setattr(
        "yunpai_orchestrator.worker.assembler.bridge_payload",
        lambda state, tool: {},
    )


def _solve_payload(scenario_id="SC-GRAPH-APPLY"):
    payload = _payload()
    payload.update({
        "idempotency_key": "GRAPH-APPLY-1",
        "scenario_id": scenario_id,
        "orders": [{**payload["orders"][0], "order_id": "SO-GRAPH-APPLY-1"}],
    })
    return payload


def test_apply_gate_persists_m5_release_and_head(tmp_path, monkeypatch):
    m5_db = tmp_path / "m5.sqlite"
    monkeypatch.setenv("YUNPAI_M5_DB", str(m5_db))
    _no_bridge(monkeypatch)

    repo = InMemoryRunRepository()
    graph = build_graph(_deps(repository=repo), checkpointer=MemorySaver())
    state = _free_solve_state(_solve_payload())
    config = _config(state)

    out = asyncio.run(graph.ainvoke(state, config))
    saved = repo.get(state["run_id"])
    assert saved and saved.get("pending_gate"), "draft 求解必须挂起在 apply 门"
    assert saved["pending_gate"]["type"] == "apply"
    assert saved["pending_gate"]["tool"] == "solve_scheduling"

    # 发布前：库内只有 draft，head 尚未推进
    plan_version = out["outputs"]["solve_scheduling"]["data"]["plan_version"]
    repository = M5Repository(m5_db)
    assert repository.get_plan(plan_version)["lifecycle_status"] == "draft"
    assert repository.get_head("SC-GRAPH-APPLY") is None

    resumed = asyncio.run(graph.ainvoke(
        Command(resume={"decision": "approve", "actor": "pm", "roles": ["production-manager"]}),
        config))
    assert resumed["status"] == "completed", f"errors={resumed.get('errors')}"
    released = resumed["outputs"]["solve_scheduling"]["data"]
    assert released["lifecycle_status"] == "released"
    assert released["head_revision"] == 1

    plan = repository.get_plan(plan_version)
    assert plan["lifecycle_status"] == "released"
    assert plan["released_at"]
    head = repository.get_head("SC-GRAPH-APPLY")
    assert head["head_plan_version"] == plan_version
    assert head["revision"] == 1
    assert [event["to_status"] for event in repository.lifecycle_events(plan_version)] == \
        ["approved", "released"]
    # 发布动作留痕：trace + evidence 都指向真实库发布
    assert any(entry.get("event") == "m5.released" for entry in resumed.get("trace") or [])
    assert any("release" in str(item.get("evidence_ref", ""))
               for item in resumed["outputs"]["solve_scheduling"].get("evidence") or [])


def test_apply_gate_head_conflict_reopens_gate_without_touching_state(tmp_path, monkeypatch):
    """head CAS 冲突不得抛异常、也不得静默完成：撤回授权 + 步骤退回 pending，
    apply 门重新打开（人工可恢复），且库内计划仍是 draft。"""
    m5_db = tmp_path / "m5.sqlite"
    monkeypatch.setenv("YUNPAI_M5_DB", str(m5_db))
    _no_bridge(monkeypatch)

    graph = build_graph(_deps(), checkpointer=MemorySaver())
    state = _free_solve_state(_solve_payload("SC-GRAPH-CONFLICT"), expected_head_revision=7)
    config = _config(state)

    out = asyncio.run(graph.ainvoke(state, config))
    assert out.get("__interrupt__"), "应先挂起在 apply 门"
    plan_version = out["outputs"]["solve_scheduling"]["data"]["plan_version"]

    resumed = asyncio.run(graph.ainvoke(
        Command(resume={"decision": "approve", "actor": "pm", "roles": ["production-manager"]}),
        config))
    # 冲突 → 门重新打开（而不是 completed / failed / 抛异常）
    assert resumed.get("__interrupt__"), "冲突后 apply 门必须重新打开，保持人工可恢复"
    info = resumed["__interrupt__"][0]
    info = info.value if hasattr(info, "value") else info
    assert info.get("type") == "gate_pending"
    assert info["gate"]["type"] == "apply"
    assert any(entry.get("event") == "m5.release_conflict" for entry in resumed.get("trace") or [])
    repository = M5Repository(m5_db)
    assert repository.get_plan(plan_version)["lifecycle_status"] == "draft"
    assert repository.get_head("SC-GRAPH-CONFLICT") is None
    # 门通道未卡死：后续合法 reject 能送达并结束 run
    rejected = asyncio.run(graph.ainvoke(
        Command(resume={"decision": "reject", "actor": "pm", "roles": ["production-manager"]}),
        config))
    assert rejected["status"] == "failed"
    assert any(e.get("code") == "GATE_REJECTED" for e in rejected.get("errors") or [])
