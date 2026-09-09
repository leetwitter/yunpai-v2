"""审查规则 parity：``reviewer/rules.py`` ↔ 旧 ``legacy/agents.py:388-449`` 行为对照。

**分区约定（append-only）**：本文件按分片分段，各分片只**追加自己的段**，不要重排或改写
别段（避免 6 个分片同时新建/改写同名文件时互相冲突）。当前包含：

- **M3 段（R6 批次）** —— 审批四件的合同门 + ``run_m3_procurement_requirements`` 的
  ``BLOCKED_INPUT`` 覆盖与「无 lines/shortage_lines → data 门」的 parity 结论。

旧行为对照（``legacy/agents.py``）：

| 旧行为 | 行 | V2 落点 |
|---|---|---|
| ``success is False or code == BLOCKED_INPUT`` → data 门 | :395-397 | ``rules.evaluate`` 的 BLOCKED_INPUT 统一门（``rules.py:115-118``） |
| ``run_m3_procurement_requirements`` 无 lines/shortage_lines → data 门 | :440-441 | 本地 handler 在该状态下返回 ``BLOCKED_INPUT``（``workers.m3_mrp``）→ 同上统一门 |
| 写类自由工具执行前 authorization 门 | :377-385 | 合同 ``review_gate``：M3 审批四件 = ``authorization``（manifest 声明） |
| M3 审批四件无结果级规则 | — | 与旧版一致：门是合同级（授权），不是结果级 |

**已知残余缺口（登记在 REQUESTS-MIG-M3.md）**：若远端 M3 服务返回 ``success=true`` 且
``lines``/``shortage_lines`` 皆空（旧版会开 data 门），V2 的 ``evaluate`` 不会开门——因为
``gate_type_for`` 把 RULES 里任何 ``gate:`` 动作都当作**工具级**合同门，给 ``run_m3`` 加结果级
规则会让主链被预门拦。修法需要拆分「合同门 / 结果门」（``reviewer/rules.py`` 语义扩展，M-INFRA 口径）。
"""
from __future__ import annotations

import pytest

from yunpai_orchestrator.reviewer import rules
from yunpai_orchestrator.reviewer.gates import (
    GATE_ALLOWED_ROLES,
    GATE_DECISIONS,
    GateError,
    make_gate,
    validate_resume_decision,
)
from yunpai_orchestrator.registry import build_default_registry

#: M3 审批四件（旧审批族）：外部写入 → 必须人工授权（旧 agents.py:377-385 语义）。
M3_APPROVAL_TOOLS = (
    "approve_m3_task",
    "reject_m3_task",
    "request_change_m3_task",
    "approve_to_send_m3_task",
)

#: M3 只读/恒空/恒失败查询（无门；只读语义）。
M3_NO_GATE_TOOLS = (
    "list_m3_orders",
    "get_m3_order",
    "get_m3_procurement_plan",
    "get_persisted_m3_plan",
    "get_material_readiness",
    "get_pr_po_drafts",
    "get_m3_approval_tasks",
    "get_m3_m4_handoffs",
    "export_m3_procurement_suggestions",
    "get_material_readiness_snapshot",
    "run_m3_procurement_requirements",
    "receive_m3_material_demand",
)


@pytest.fixture(scope="module")
def registry():
    return build_default_registry()


def test_m3_approval_tools_gate_is_authorization_not_blocked_input(registry):
    """疑点 ①：审批动作过去被 ``_REVIEW_GATE_MAP["data"]`` 归一成「补数门」，现改 authorization。"""
    for name in M3_APPROVAL_TOOLS:
        spec = registry.specs[name]
        assert rules.gate_type_for(name, spec) == "authorization", name
        assert rules.gate_type_for(name, spec) != "blocked_input", name


def test_m3_approval_gate_declared_in_contract(registry):
    for name in M3_APPROVAL_TOOLS:
        spec = registry.specs[name]
        assert spec.side_effect == "external_write", name
        assert spec.review_gate == "authorization", name


def test_m3_authorization_gate_role_and_decision_matrix():
    gate = make_gate("authorization", "approve_m3_task", "写动作需人工授权")
    assert set(gate["allowed_roles"]) == set(GATE_ALLOWED_ROLES["authorization"])
    assert {"operator", "admin"} <= set(gate["allowed_roles"])
    assert validate_resume_decision("authorization", "approve") == "approve"
    assert validate_resume_decision("authorization", "reject") == "reject"
    with pytest.raises(GateError):
        validate_resume_decision("authorization", "retry")   # 授权门不接受补数重试
    assert GATE_DECISIONS["authorization"] == ("approve", "reject")


def test_m3_run_m3_blocked_input_opens_data_gate(registry):
    """parity：旧 agents.py:440-441「无 lines/shortage_lines → data 门」由统一 BLOCKED_INPUT 门覆盖。"""
    spec = registry.specs["run_m3_procurement_requirements"]
    findings = rules.evaluate(
        "run_m3_procurement_requirements",
        {"success": False, "code": "BLOCKED_INPUT", "data": {"lines": [], "shortage_lines": []}},
        spec,
    )
    assert findings and findings[0]["gate"] == "blocked_input"
    assert "retry" in GATE_DECISIONS["blocked_input"]


def test_m3_run_m3_has_no_unconditional_default_gate(registry):
    """主链不得被预门拦：run_m3 只在缺输入（BLOCKED_INPUT）时开门。"""
    spec = registry.specs["run_m3_procurement_requirements"]
    assert rules.gate_type_for("run_m3_procurement_requirements", spec) == ""


def test_m3_other_tools_have_no_contract_gate(registry):
    for name in M3_NO_GATE_TOOLS:
        assert rules.gate_type_for(name, registry.specs[name]) == "", name


def test_m3_readonly_results_never_open_gates(registry):
    """只读/空结果不得开门（AUTO_APPROVE_ALLOWED=False 也不得因此降级）。"""
    spec = registry.specs["list_m3_orders"]
    assert rules.evaluate("list_m3_orders", {"success": True, "data": []}, spec) == []
    assert rules.AUTO_APPROVE_ALLOWED is False
