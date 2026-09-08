"""书二 §3：LangGraph 唯一执行路径——冒烟：chat / free 工具 / Gate interrupt 往返 / 记忆。"""
import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from yunpai_orchestrator.checkpointer import memory_checkpointer
from yunpai_orchestrator.config import OrchestratorConfig
from yunpai_orchestrator.graph import GraphDeps, build_graph, default_deps
from yunpai_orchestrator.llm import QwenConfig, QwenRouter
from yunpai_orchestrator.orchestrator.router import Router
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.reviewer import rules
from yunpai_orchestrator.skills import build_default_skill_registry
from yunpai_orchestrator.state import new_state_v2


def _deps(repository=None) -> GraphDeps:
    cfg = OrchestratorConfig()
    reg = build_default_registry()
    skills = build_default_skill_registry()
    skills.validate_tools(reg.specs)
    engine = default_deps(cfg, registry=reg, skills=skills).engine
    return GraphDeps(
        registry=reg, skills=skills,
        router=Router(QwenRouter(QwenConfig(enabled=False)), reg, skills, engine),
        planner=default_deps(cfg, registry=reg, skills=skills).planner,
        engine=engine, assembler=default_deps(cfg, registry=reg, skills=skills).assembler,
        config=cfg, repository=repository,
    )


def _run(graph, state, limit=32):
    import asyncio
    return asyncio.run(graph.ainvoke(
        state, {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": limit}))


def test_chat_route_end_to_end():
    graph = build_graph(_deps(), checkpointer=memory_checkpointer())
    state = new_state_v2({"message": "你能做什么"})
    out = _run(graph, state)
    assert out["status"] == "completed"
    assert out["route"] == "chat"
    assert len(out["messages"]) == 2  # user + assistant（记忆真实写入，治痛点 11）


def test_free_route_single_tool_completes():
    graph = build_graph(_deps(), checkpointer=memory_checkpointer())
    state = new_state_v2({"message": "查计划列表", "tools": ["list_m5_schedules"],
                          "list_m5_schedules": {"limit": 5}})
    out = _run(graph, state, limit=48)
    assert out["route"] == "free"
    assert out["status"] in ("completed", "failed")
    assert out["outputs"].get("list_m5_schedules"), "工具应被执行并产出信封"


def test_gate_interrupt_and_resume_roundtrip():
    """Gate：interrupt 挂起 → waiting_human 落库 → Command(resume=approve) → 授权完成。"""
    rules.RULES["list_m5_schedules"] = [
        rules.Check("status", "eq", "success", action="gate:authorization",
                    reason="测试注入门")]
    try:
        from yunpai_orchestrator.repository import InMemoryRunRepository
        repo = InMemoryRunRepository()
        graph = build_graph(_deps(repository=repo), checkpointer=MemorySaver())
        state = new_state_v2({"message": "查计划", "tools": ["list_m5_schedules"]})
        config = {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": 48}
        import asyncio
        out = asyncio.run(graph.ainvoke(state, config))
        # 挂起：仓库镜像带 pending_gate（interrupt 前 side-effect 落库）
        saved = repo.get(state["run_id"])
        assert saved and saved.get("pending_gate"), "Gate 挂起必须先落库可见"
        assert saved["pending_gate"]["type"] == "authorization"
        # resume：admin approve
        resumed = asyncio.run(graph.ainvoke(
            Command(resume={"decision": "approve", "actor": "tester", "roles": ["admin"]}),
            config))
        assert resumed["status"] == "completed"
        assert "list_m5_schedules" in resumed.get("authorized_steps", [])
        assert resumed["approvals"], "审批必须留痕"
    finally:
        rules.RULES.pop("list_m5_schedules", None)


def test_gate_reject_fails_run():
    rules.RULES["list_m5_schedules"] = [
        rules.Check("status", "eq", "success", action="gate:authorization", reason="测试注入门")]
    try:
        graph = build_graph(_deps(), checkpointer=MemorySaver())
        state = new_state_v2({"message": "查计划", "tools": ["list_m5_schedules"]})
        config = {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": 48}
        import asyncio
        asyncio.run(graph.ainvoke(state, config))
        resumed = asyncio.run(graph.ainvoke(
            Command(resume={"decision": "reject", "actor": "t", "roles": ["admin"]}), config))
        assert resumed["status"] == "failed"
        assert any(e.get("code") == "GATE_REJECTED" for e in resumed.get("errors", []))
    finally:
        rules.RULES.pop("list_m5_schedules", None)


def test_memory_thread_continuity():
    """同 thread_id 两次 run：第二次能看到第一轮的 assistant 消息（跨 run 记忆）。"""
    saver = MemorySaver()
    graph = build_graph(_deps(), checkpointer=saver)
    import asyncio
    s1 = new_state_v2({"message": "第一轮"})
    config1 = {"configurable": {"thread_id": "thread-fixed-1"}, "recursion_limit": 24}
    out1 = asyncio.run(graph.ainvoke(s1, config1))
    assert len(out1["messages"]) == 2
    s2 = new_state_v2({"message": "第二轮"})
    out2 = asyncio.run(graph.ainvoke(s2, config1))
    assert len(out2["messages"]) >= 4  # 前轮记忆 + 本轮 user/assistant


def test_no_manual_loop_guard():
    """架构守护：v2 包内不得存在旧式 YunpaiGraph 手写执行循环类。"""
    import yunpai_orchestrator.graph as g
    assert not hasattr(g, "YunpaiGraph")
    assert not hasattr(g, "CompatGraph")
