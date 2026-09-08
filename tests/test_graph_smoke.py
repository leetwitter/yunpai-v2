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


def test_gate_invalid_decision_reprompts_without_deadlock():
    """非法决策不抛异常、不卡死 resume 通道：再次挂起（gate_invalid 提示）→
    后续合法 resume 正常送达（langgraph 会固化抛错 resume 值，必须绕开）。"""
    rules.RULES["list_m5_schedules"] = [
        rules.Check("status", "eq", "success", action="gate:blocked_input", reason="测试注入数据门")]
    try:
        import asyncio
        from langgraph.types import Interrupt

        graph = build_graph(_deps(), checkpointer=MemorySaver())
        state = new_state_v2({"message": "查计划", "tools": ["list_m5_schedules"]})
        config = {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": 64}

        out = asyncio.run(graph.ainvoke(state, config))
        assert "__interrupt__" in out, "应挂起在 blocked_input 门"

        # ① 非法决策（blocked_input 不接受 approve）：不抛异常，再次挂起并带提示
        out2 = asyncio.run(graph.ainvoke(
            Command(resume={"decision": "approve", "actor": "t", "roles": ["admin"]}), config))
        assert "__interrupt__" in out2, "非法决策应再次挂起而非抛错"
        info = out2["__interrupt__"][0].value if isinstance(out2["__interrupt__"][0], Interrupt) \
            else out2["__interrupt__"][0]
        assert info.get("type") == "gate_invalid"
        assert "approve" in str(info.get("error"))

        # ② 合法 retry 正常送达：步骤重派发 → 工具重跑 → 门再次打开（结果未变）
        out3 = asyncio.run(graph.ainvoke(
            Command(resume={"decision": "retry", "actor": "t", "roles": ["admin"]}), config))
        assert "__interrupt__" in out3, "retry 重派发后门应再次打开"

        # ③ 合法 reject 送达：run 失败并留痕
        out4 = asyncio.run(graph.ainvoke(
            Command(resume={"decision": "reject", "actor": "t", "roles": ["admin"]}), config))
        assert out4["status"] == "failed"
        assert any(e.get("code") == "GATE_REJECTED" for e in out4.get("errors", []))
    finally:
        rules.RULES.pop("list_m5_schedules", None)


def test_engineering_gate_rule_fires_on_m2_draft():
    """M2 草稿（success=True）→ 规则表开 engineering 门（M2→M3 只消费已批准 BOM 的入口）。"""
    finding = next(
        (f for f in rules.evaluate("run_bom_sop_workflow", {"success": True, "status": "draft_created"})
         if f.get("gate") == "engineering"), None)
    assert finding, "m2 草稿应触发 engineering 门"
    assert not rules.evaluate("run_bom_sop_workflow", {"success": None}), "无 success 不触发"


def test_engineering_approval_stamp():
    """工程门 approve 的 outputs 落点：bom/sop generation 写 approval_status=approved
    （顶层与 data 嵌套两份都盖）；非 engineering 门不动 outputs。"""
    from yunpai_orchestrator.graph import _stamp_engineering_approval

    state = {"outputs": {"run_bom_sop_workflow": {
        "status": "draft_created",
        "bom_generation": {"bom_lines": [{"material_code": "M-1"}]},
        "sop_generation": {"status": "draft"},
        "data": {"bom_generation": {"bom_lines": [{"material_code": "M-1"}]},
                 "sop_generation": {"status": "draft"}}}}}
    stamped = _stamp_engineering_approval(state, {"type": "engineering", "tool": "run_bom_sop_workflow"})
    assert stamped is not None
    env = stamped["run_bom_sop_workflow"]
    for holder in (env, env["data"]):
        assert holder["bom_generation"]["approval_status"] == "approved"
        assert holder["sop_generation"]["approval_status"] == "approved"
    # 原状态不被原地篡改（LangGraph 节点返回增量语义）
    assert "approval_status" not in state["outputs"]["run_bom_sop_workflow"]["bom_generation"]
    # 非 engineering 门 / 无可盖章节 → None
    assert _stamp_engineering_approval(state, {"type": "authorization", "tool": "run_bom_sop_workflow"}) is None
    assert _stamp_engineering_approval({"outputs": {"x": {}}}, {"type": "engineering", "tool": "x"}) is None


def test_tool_exception_fails_run_not_silent_complete():
    """工具异常（无 fail 规则）不得被静默标 completed：重试耗尽后 run 应 failed。"""
    import asyncio

    deps = _deps()

    async def boom(tool, payload, ctx):
        raise RuntimeError("synthetic boom")

    deps.registry.call = boom
    graph = build_graph(deps, checkpointer=MemorySaver())
    state = new_state_v2({"message": "x", "tools": ["query_recognized_table"],
                          "query_recognized_table": {}})
    out = asyncio.run(graph.ainvoke(
        state, {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": 64}))
    assert out["status"] == "failed"
    step = [p for p in out["plan"] if p.get("tool") == "query_recognized_table"][0]
    assert step["status"] == "failed"
    assert out["retry_counts"].get("tool-query_recognized_table") == deps.max_step_retries
    assert any(e.get("code") == "UPSTREAM_UNAVAILABLE" for e in out.get("errors", []))


def test_tool_exception_retry_recovers_to_completed():
    """瞬态异常：重试余量内恢复 → run completed（验证异常重试路径正向）。"""
    import asyncio

    calls = {"n": 0}
    deps = _deps()

    async def flaky(tool, payload, ctx):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return {"success": True, "data": {"ok": True}}

    deps.registry.call = flaky
    graph = build_graph(deps, checkpointer=MemorySaver())
    state = new_state_v2({"message": "x", "tools": ["query_recognized_table"],
                          "query_recognized_table": {}})
    out = asyncio.run(graph.ainvoke(
        state, {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": 64}))
    assert out["status"] == "completed"
    assert calls["n"] == 3  # 1 次初始 + 2 次重试


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
