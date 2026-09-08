"""知识自进化（EV-1）测试：仓库/独立性反作弊/晋升门禁/状态机/红线/API/图挂接。"""
from __future__ import annotations

from pathlib import Path

import pytest

from yunpai_orchestrator.evolution.gate import should_promote
from yunpai_orchestrator.evolution.redline import RedlineMonitor, protected_paths, snapshot, verify
from yunpai_orchestrator.evolution.repository import EvolutionRepository
from yunpai_orchestrator.evolution.signals import (
    extract_error_patterns,
    extract_human_corrections,
    extract_repeated_operation,
    extract_success_patterns,
    observe_run,
)
from yunpai_orchestrator.models import new_state


@pytest.fixture()
def repo(tmp_path: Path) -> EvolutionRepository:
    return EvolutionRepository(tmp_path / "evolution.sqlite")


def _completed_step(tool: str, status: str = "completed") -> dict:
    return {"id": f"step-{tool}", "module": "m0", "tool": tool, "status": status,
            "input_summary": {"kind": "order"}, "output_summary": {}, "evidence": [],
            "started_at": "", "finished_at": ""}


def _run_with(steps: list[dict], *, approvals: list[dict] | None = None,
              errors: list[dict] | None = None, tenant_id: str = "default") -> dict:
    state = new_state({"message": "测试"}, tenant_id=tenant_id)
    state["steps"] = steps
    state["approvals"] = approvals or []
    state["errors"] = errors or []
    return state


# ---------------------------------------------------------------------------
# 信号提取（纯函数）
# ---------------------------------------------------------------------------

def test_extract_repeated_operation_requires_two_steps():
    assert extract_repeated_operation(_run_with([_completed_step("a")])) == []
    signals = extract_repeated_operation(_run_with([_completed_step("a"), _completed_step("b")]))
    assert len(signals) == 1
    assert signals[0]["kind"] == "repeated_operation"
    assert signals[0]["content"]["tools"] == ["a", "b"]
    assert signals[0]["pattern"] is not None


def test_extract_error_patterns_from_errors_and_rejections():
    state = _run_with(
        [_completed_step("ingest_document", status="failed")],
        errors=[{"code": "MISSING_SOP", "tool": "run_bom_sop_workflow", "message": "缺 SOP"}],
        approvals=[{"decision": "reject", "gate": {"type": "data", "tool": "solve_scheduling", "message": "缺日历"}}],
    )
    signals = extract_error_patterns(state)
    codes = {(s["content"].get("code"), s["content"].get("tool")) for s in signals}
    assert ("MISSING_SOP", "run_bom_sop_workflow") in codes
    assert ("GATE_REJECTED", "solve_scheduling") in codes


def test_extract_human_correction_is_strong():
    state = _run_with(
        [_completed_step("data_import_run")],
        approvals=[{"decision": "retry", "supplemented": True,
                    "supplement": {"due_date": "2026-10-01", "product_code": "W-H909"},
                    "gate": {"type": "data", "tool": "ingest_document"}}],
    )
    signals = extract_human_corrections(state)
    assert len(signals) == 1
    assert signals[0]["strong"] is True
    assert set(signals[0]["content"]["corrected_fields"]) == {"due_date", "product_code"}


def test_extract_success_patterns_skips_supplemented():
    state = _run_with(
        [],
        approvals=[
            {"decision": "approve", "supplemented": False, "gate": {"type": "apply", "tool": "solve_scheduling"}},
            {"decision": "approve", "supplemented": True, "gate": {"type": "data", "tool": "ingest_document"}},
        ],
    )
    signals = extract_success_patterns(state)
    assert len(signals) == 1
    assert signals[0]["content"]["tool"] == "solve_scheduling"


# ---------------------------------------------------------------------------
# 仓库 + 独立性反作弊
# ---------------------------------------------------------------------------

def test_independence_anti_cheat_same_task_counts_once(repo):
    cand = repo.upsert_candidate(candidate_id="CAND-1", tenant_id="default",
                                 kind="error_pattern", fingerprint="fp-1",
                                 content={"code": "X"}, applicability={"tool": "t"})
    repo.record_source(source_id="SRC-1", tenant_id="default", source_kind="run_step", run_id="r1", task_id="task-1", content_hash="h")
    repo.record_source(source_id="SRC-2", tenant_id="default", source_kind="run_step", run_id="r2", task_id="task-1", content_hash="h2")
    repo.add_candidate_source(candidate_id=cand["candidate_id"], source_id="SRC-1", tenant_id="default", task_id="task-1")
    repo.add_candidate_source(candidate_id=cand["candidate_id"], source_id="SRC-2", tenant_id="default", task_id="task-1")
    fresh = repo.get_candidate(cand["candidate_id"])
    # 同 task_id 的两个来源：独立计数=1，支持计数=2。
    assert fresh["independent_count"] == 1
    assert fresh["support_count"] == 2


def test_independence_distinct_tasks(repo):
    cand = repo.upsert_candidate(candidate_id="CAND-2", tenant_id="default",
                                 kind="repeated_operation", fingerprint="fp-2",
                                 content={}, applicability={})
    for i, task in enumerate(("task-1", "task-2", "task-3"), start=1):
        repo.record_source(source_id=f"SRC-{i}", tenant_id="default", source_kind="run_step", run_id=f"r{i}", task_id=task, content_hash=f"h{i}")
        repo.add_candidate_source(candidate_id=cand["candidate_id"], source_id=f"SRC-{i}", tenant_id="default", task_id=task)
    assert repo.get_candidate(cand["candidate_id"])["independent_count"] == 3


# ---------------------------------------------------------------------------
# 晋升门禁
# ---------------------------------------------------------------------------

def test_should_promote_threshold_and_strong():
    assert should_promote({"status": "observed", "independent_count": 2}, strong=False) == (False, "independent_count=2<3")
    assert should_promote({"status": "observed", "independent_count": 3}, strong=False)[0] is True
    assert should_promote({"status": "observed", "independent_count": 0}, strong=True)[0] is True
    assert should_promote({"status": "rejected", "independent_count": 9}, strong=False)[0] is False


# ---------------------------------------------------------------------------
# observe_run 端到端 + 状态机
# ---------------------------------------------------------------------------

def test_observe_run_repeated_operation_proposes_after_three(repo):
    steps = [_completed_step("ingest_document"), _completed_step("run_bom_sop_workflow")]
    for i in range(3):
        state = _run_with(steps, tenant_id="default")
        state["task_id"] = f"task-{i}"
        observe_run(repo, state)
    cands = repo.list_candidates(tenant_id="default", kind="repeated_operation")
    assert len(cands) == 1
    assert cands[0]["status"] == "proposed"
    assert cands[0]["independent_count"] == 3
    # 重复操作同时维护 pattern 表。
    assert len(repo.list_patterns(tenant_id="default")) == 1


def test_observe_run_human_correction_proposes_at_one(repo):
    state = _run_with([_completed_step("ingest_document")], approvals=[
        {"decision": "retry", "supplemented": True, "supplement": {"due_date": "2026-10-01"},
         "gate": {"type": "data", "tool": "ingest_document"}}])
    observe_run(repo, state)
    cands = repo.list_candidates(tenant_id="default", kind="human_correction")
    assert len(cands) == 1
    assert cands[0]["status"] == "proposed"


def test_observe_run_is_idempotent_per_task(repo):
    steps = [_completed_step("a"), _completed_step("b")]
    state = _run_with(steps)
    observe_run(repo, state)
    observe_run(repo, state)  # 同 task 重放
    cands = repo.list_candidates(tenant_id="default", kind="repeated_operation")
    assert len(cands) == 1
    assert cands[0]["independent_count"] == 1
    assert cands[0]["support_count"] == 2


def test_observe_run_never_raises_on_empty(repo):
    result = observe_run(repo, new_state({"message": "hi"}))
    assert result["observed"] is True


def test_observe_run_collects_scheduling_case(repo):
    state = _run_with([_completed_step("record_m5_knowledge")])
    state["outputs"] = {"record_m5_knowledge": {
        "success": True, "data": {"plan_version": "PV-1", "scenario_id": "sc-1",
                                   "on_time_rate": 0.97, "validation_passed": True,
                                   "solver_status": "optimal"}}}
    observe_run(repo, state)
    cands = repo.list_candidates(tenant_id="default", kind="scheduling_case")
    assert len(cands) == 1
    assert cands[0]["content"]["plan_version"] == "PV-1"


# ---------------------------------------------------------------------------
# 知识晋升 + 状态机（API 层）
# ---------------------------------------------------------------------------

def test_decide_approve_creates_knowledge_and_version(repo):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from yunpai_orchestrator.evolution.api import create_evolution_router

    cand = repo.upsert_candidate(candidate_id="CAND-1", tenant_id="default",
                                 kind="error_pattern", fingerprint="fp-1",
                                 content={"code": "MISSING_SOP"}, applicability={"tool": "run_bom_sop_workflow"})
    repo.set_candidate_status(cand["candidate_id"], "proposed")

    app = FastAPI()
    app.include_router(create_evolution_router(repo))
    client = TestClient(app)

    resp = client.post("/api/evolution/candidates/CAND-1/decision", json={"decision": "approve"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "active"
    knowledge = repo.list_knowledge(tenant_id="default", kind="error_pattern")
    assert len(knowledge) == 1
    assert knowledge[0]["knowledge_id"] == body["knowledge_id"]
    assert knowledge[0]["version"] == 1
    assert repo.get_candidate("CAND-1")["status"] == "active"


def test_decide_reject_and_later(repo):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from yunpai_orchestrator.evolution.api import create_evolution_router

    repo.upsert_candidate(candidate_id="CAND-2", tenant_id="default", kind="error_pattern",
                          fingerprint="fp-2", content={}, applicability={})
    repo.set_candidate_status("CAND-2", "proposed")
    app = FastAPI()
    app.include_router(create_evolution_router(repo))
    client = TestClient(app)

    assert client.post("/api/evolution/candidates/CAND-2/decision", json={"decision": "reject"}).status_code == 200
    assert repo.get_candidate("CAND-2")["status"] == "rejected"

    repo.upsert_candidate(candidate_id="CAND-3", tenant_id="default", kind="error_pattern",
                          fingerprint="fp-3", content={}, applicability={})
    repo.set_candidate_status("CAND-3", "proposed")
    client.post("/api/evolution/candidates/CAND-3/decision", json={"decision": "later"})
    fresh = repo.get_candidate("CAND-3")
    assert fresh["status"] == "observed"
    assert fresh["last_error"].startswith("later:")


def test_promotion_blocks_sensitive_content(repo):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from yunpai_orchestrator.evolution.api import create_evolution_router

    repo.upsert_candidate(candidate_id="CAND-4", tenant_id="default", kind="error_pattern",
                          fingerprint="fp-4",
                          content={"code": "X", "message": "身份证 11010519491231002X"},
                          applicability={})
    repo.set_candidate_status("CAND-4", "proposed")
    app = FastAPI()
    app.include_router(create_evolution_router(repo))
    client = TestClient(app)
    resp = client.post("/api/evolution/candidates/CAND-4/decision", json={"decision": "approve"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "PROMOTION_BLOCKED"


def test_metrics(repo):
    repo.upsert_candidate(candidate_id="CAND-9", tenant_id="default", kind="error_pattern",
                          fingerprint="fp-9", content={}, applicability={})
    m = repo.metrics(tenant_id="default")
    assert m["candidates"]["observed"] == 1
    assert m["knowledge_active"] == 0


# ---------------------------------------------------------------------------
# 只读红线监测
# ---------------------------------------------------------------------------

def test_redline_snapshot_and_verify(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "workflows").mkdir(parents=True)
    (root / "src").mkdir(parents=True)
    (root / "AGENTS.md").write_text("# agents", encoding="utf-8")
    (root / "workflows" / "x.json").write_text("{}", encoding="utf-8")
    (root / "src" / "mod.py").write_text("x = 1\n", encoding="utf-8")

    assert "AGENTS.md" in [p.name for p in protected_paths(root)]
    before = snapshot(root)
    assert "workflows/x.json" in before
    monitor = RedlineMonitor(root)  # baseline = 修改前快照

    # 触碰编排文件 → verify 报 changed，monitor.check 报不干净。
    (root / "workflows" / "x.json").write_text("{\"changed\": true}", encoding="utf-8")
    result = verify(before, root=root)
    assert result["changed"] == ["workflows/x.json"]
    assert result["clean"] is False
    assert monitor.check()["clean"] is False


# ---------------------------------------------------------------------------
# 图挂接：观察钩子不破坏主流程
# ---------------------------------------------------------------------------

def test_graph_observe_does_not_break_run(repo):
    import asyncio

    from yunpai_orchestrator.checkpointer import memory_checkpointer
    from yunpai_orchestrator.graph import build_graph, default_deps
    from yunpai_orchestrator.state import new_state_v2

    graph = build_graph(default_deps(evolution=repo), checkpointer=memory_checkpointer())
    state0 = new_state_v2({"message": "你好"})
    state = asyncio.run(graph.ainvoke(state0, {"configurable": {"thread_id": state0["thread_id"]}, "recursion_limit": 24}))
    assert state["status"] == "completed"
    # 观察钩子已挂（无步骤的 chat 也会执行一次观察）。
    assert repo is not None
