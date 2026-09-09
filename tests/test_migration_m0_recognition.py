"""rows-S0「测试需补」：`ingest_canonical` 的端到端「落库 → candidate Gate → approve」。

rows-S0 判定：V2 `test_review_rules_gates.py` 无 `ingest_canonical` 用例，
`test_canonical_ingest.py:80-84` 只直调 `registry.call`，**无端到端门断言**。
本文件用 LangGraph 真实跑一遍：候选落库 → 开 candidate 门 → resume approve/reject。

另含 rows-S1 的 `data_import_run` candidate 门回归：**门批准即落地候选裁决**
（`_apply_candidate_approval`），否则 `data_import_commit` 会 PENDING_REVIEW 死锁（W913）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from yunpai_orchestrator.canonical_ingest import CanonicalLandingStore
from yunpai_orchestrator.checkpointer import memory_checkpointer
from yunpai_orchestrator.config import OrchestratorConfig
from yunpai_orchestrator.graph import GraphDeps, build_graph, default_deps
from yunpai_orchestrator.llm import QwenConfig, QwenRouter
from yunpai_orchestrator.orchestrator.router import Router
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.reviewer import rules
from yunpai_orchestrator.skills import build_default_skill_registry
from yunpai_orchestrator.state import new_state_v2

TOOL = "ingest_canonical"


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


def _payload() -> dict:
    return {
        "entity_type": "material",
        "records": [{"material_code": "M-GATE-1", "material_name": "门测试物料"}],
        "filename": "material.xlsx",
        "sha256": hashlib.sha256(b"gate-roundtrip").hexdigest(),
        "confidence": 0.93,
    }


@pytest.fixture
def canonical_db(tmp_path, monkeypatch):
    db = tmp_path / "canonical.sqlite"
    monkeypatch.setenv("YUNPAI_CANONICAL_DB", str(db))
    monkeypatch.delenv("M0_URL", raising=False)
    return db


def test_ingest_canonical_result_opens_candidate_gate(canonical_db):
    """直调 registry：落库成功信封 → RULES 判 candidate 门（门不因 success 而消失）。"""
    registry = build_default_registry()
    result = asyncio.run(registry.call(
        TOOL, _payload(),
        {"task_id": "TASK-GATE-1", "tenant_id": "default", "canonical_db": str(canonical_db)}))

    assert result["success"] is True
    assert result["data"]["inserted_rows"] == 1
    assert result["data"]["duplicate"] is False

    findings = rules.evaluate(TOOL, result, registry.specs[TOOL])
    gate_finding = next((f for f in findings if f.get("gate")), None)
    assert gate_finding is not None, findings
    assert gate_finding["gate"] == "candidate"
    assert rules.gate_type_for(TOOL, registry.specs[TOOL]) == "candidate"


def test_ingest_canonical_end_to_end_gate_approve(canonical_db):
    """端到端：free 路由执行 → candidate 门挂起（落库已发生）→ approve 后 run 完成。"""
    from yunpai_orchestrator.repository import InMemoryRunRepository

    repo = InMemoryRunRepository()
    graph = build_graph(_deps(repository=repo), checkpointer=MemorySaver())
    state = new_state_v2({"message": "落库这条物料", "tools": [TOOL], TOOL: _payload()})
    config = {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": 48}

    out = asyncio.run(graph.ainvoke(state, config))
    saved = repo.get(state["run_id"])
    assert saved and saved.get("pending_gate"), "候选门挂起必须先落库可见"
    assert saved["pending_gate"]["type"] == "candidate"
    assert saved["pending_gate"]["tool"] == TOOL

    # 落库已真实发生（门是「落库后审批」，不是「审批后才落库」）
    rows = CanonicalLandingStore(str(canonical_db)).query(entity_type="material")
    assert [row["material_code"] for row in rows] == ["M-GATE-1"]

    resumed = asyncio.run(graph.ainvoke(
        Command(resume={"decision": "approve", "actor": "steward", "roles": ["m0-reviewer"]}),
        config))
    assert resumed["status"] == "completed"
    assert TOOL in resumed.get("authorized_steps", [])
    assert resumed["approvals"][-1]["gate_type"] == "candidate"
    assert resumed["outputs"][TOOL]["data"]["inserted_rows"] == 1


def test_ingest_canonical_gate_reject_fails_run(canonical_db):
    graph = build_graph(_deps(), checkpointer=MemorySaver())
    state = new_state_v2({"message": "落库这条物料", "tools": [TOOL], TOOL: _payload()})
    config = {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": 48}
    asyncio.run(graph.ainvoke(state, config))
    resumed = asyncio.run(graph.ainvoke(
        Command(resume={"decision": "reject", "actor": "steward", "roles": ["m0-reviewer"], "note": "来源可疑"}),
        config))
    assert resumed["status"] == "failed"
    assert any(e.get("code") == "GATE_REJECTED" for e in resumed.get("errors", []))


def test_ingest_canonical_gate_requires_m0_reviewer_role(canonical_db):
    """角色矩阵：candidate 门不接受无关角色（LLM/普通操作员不得自行放行）。"""
    graph = build_graph(_deps(), checkpointer=memory_checkpointer())
    state = new_state_v2({"message": "落库", "tools": [TOOL], TOOL: _payload()})
    config = {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": 48}
    asyncio.run(graph.ainvoke(state, config))
    out = asyncio.run(graph.ainvoke(
        Command(resume={"decision": "approve", "actor": "who", "roles": ["operator"]}), config))
    assert "__interrupt__" in out, "角色不足应再次挂起而非放行"
    info = out["__interrupt__"][0]
    value = getattr(info, "value", info)
    assert value.get("type") == "gate_invalid"
    assert "m0-reviewer" in str(value.get("error"))


def test_candidate_gate_approval_resolves_batch_candidates(tmp_path, monkeypatch):
    """W913 死锁修复：`data_import_run` candidate 门批准 → 批次候选落为 approved。"""
    monkeypatch.setenv("YUNPAI_M0_SANDBOX_DB", str(tmp_path / "sandbox.sqlite"))
    monkeypatch.delenv("YUNPAI_M0_DB", raising=False)
    monkeypatch.delenv("M0_URL", raising=False)

    record = {
        "schema_version": "m0.ingest.v1", "tenant_id": "default",
        "idempotency_key": "idem-material-M-GATE-2",
        "source": {"system": "m0-migration-test", "external_id": "ext-1",
                   "sha256": hashlib.sha256(b"M-GATE-2").hexdigest()},
        "entity_type": "material", "identity": {"business_key": "M-GATE-2"},
        "payload": {"name": "候选物料", "unit": "PCS"},
        "evidence": [{"key": "ev-1", "ref": "test:1"}],
        "review_status": "approved", "reviewed_by": "tester",
    }
    files = [{"kind": "order", "filename": "material.json",
              "content_b64": base64.b64encode(json.dumps(record, ensure_ascii=False).encode()).decode()}]

    graph = build_graph(_deps(), checkpointer=MemorySaver())
    # 装配走 bridge：data_import_run 的 files 来自 request.attachments(kind=order)
    state = new_state_v2({"message": "导入物料", "tools": ["data_import_run"],
                          "attachments": files})
    config = {"configurable": {"thread_id": state["thread_id"]}, "recursion_limit": 48}

    out = asyncio.run(graph.ainvoke(state, config))
    gate = getattr(out["__interrupt__"][0], "value", out["__interrupt__"][0])["gate"]
    assert gate["type"] == "candidate" and gate["tool"] == "data_import_run"
    batch_id = out["outputs"]["data_import_run"]["batch_id"]

    from yunpai_orchestrator.workers import _m0_store

    assert _m0_store({}).pending_count(batch_id) == 1, "批准前候选应处于待裁决"

    resumed = asyncio.run(graph.ainvoke(
        Command(resume={"decision": "approve", "actor": "steward", "roles": ["m0-reviewer"]}),
        config))
    assert resumed["status"] == "completed"
    approval = resumed["outputs"]["data_import_run"]["candidate_approval"]
    assert approval["resolved"] == 1 and approval["actor"] == "steward"
    # 死锁解除：pending 归零 → commit 不再 PENDING_REVIEW
    assert _m0_store({}).pending_count(batch_id) == 0
    from yunpai_orchestrator.workers import m0_commit

    commit = asyncio.run(m0_commit({"batch_id": batch_id, "require_resolved": True},
                                   {"task_id": "TASK-M0-MIG", "actor": "steward"}))
    assert commit["status"] == "fixture_recorded"  # sandbox 无 canonical 表 → 只记录意图
