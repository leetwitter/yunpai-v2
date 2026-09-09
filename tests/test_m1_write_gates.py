"""M1 写类工具的人工门（rows-S2「审查需补」4 条）。

V2 原状：``registry-manifests/m1.json`` 对 ``ingest_m1_archive`` /
``submit_m1_review`` / ``generate_m1_report`` / ``export_m1_order`` 既没有
``review_gate`` 声明，``reviewer/rules.py`` 也没有条目（名字不含
approve/commit/dispatch/write/send/publish → ``registry.py:55-62`` 判
``side_effect=none`` → ``review_gate=none``）——实测这 4 个写工具在 V2
**完全无门**。旧系统的 ``m1_tooling.M1_WRITE_SKILL_OPERATIONS`` + preflight
授权门在 V2 无等价物（V2 只有执行后审查，``graph.py:205-209`` 是唯一开门点），
故按既有写法补 ``RULES`` 条目 + manifest ``side_effect``/``review_gate``
声明，门类型统一为 ``authorization``（父会话 R8 裁决：本轮不恢复执行前门，
用后置等价门 + 契约声明，语义差异登记为已知缺口）。

红线：``AUTO_APPROVE_ALLOWED`` 必须仍为 False——门不得被 LLM 置信度绕过。
"""

from __future__ import annotations

import pytest

from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.reviewer import rules
from yunpai_orchestrator.reviewer.gates import (
    GATE_ALLOWED_ROLES,
    GATE_DECISIONS,
    GateError,
    authorize,
)

WRITE_TOOLS = ("ingest_m1_archive", "submit_m1_review",
               "generate_m1_report", "export_m1_order")


@pytest.fixture
def registry():
    return build_default_registry()


@pytest.mark.parametrize("tool,result", [
    ("ingest_m1_archive", {"status": "done"}),
    ("submit_m1_review", {"status": "done"}),
    ("generate_m1_report", {"report_status": "generated"}),
    ("export_m1_order", {"data": {"generated": True}}),
])
def test_m1_write_tools_open_authorization_gate(tool, result):
    findings = rules.evaluate(tool, result)
    assert [finding["gate"] for finding in findings] == ["authorization"]
    assert findings[0]["reason"]


def test_m1_write_tools_open_no_gate_on_failure_or_noop():
    assert rules.evaluate("ingest_m1_archive", {"status": "failed"}) == []
    assert rules.evaluate("submit_m1_review", {"status": "failed"}) == []
    assert rules.evaluate("generate_m1_report", {"report_status": ""}) == []
    assert rules.evaluate("export_m1_order", {"data": {"generated": False}}) == []


@pytest.mark.parametrize("tool", WRITE_TOOLS)
def test_m1_write_tool_contract_and_rules_agree(registry, tool):
    """manifest 的 side_effect/review_gate 与 RULES 条目归一化到同一道门。"""
    spec = registry.specs[tool]
    assert spec.side_effect == "local_write"
    assert spec.review_gate == "authorization"
    assert rules.gate_type_for(tool, spec) == "authorization"
    # 未授权时合同默认门不放行（必须走人工）。
    assert rules.default_gate_for_authorized(tool, spec, []) is False
    assert rules.default_gate_for_authorized(tool, spec, [tool]) is True


def test_m1_authorization_gate_keeps_role_matrix_and_decisions():
    assert "operator" in GATE_ALLOWED_ROLES["authorization"]
    assert GATE_DECISIONS["authorization"] == ("approve", "reject")
    with pytest.raises(GateError):
        authorize({"type": "authorization"}, ["document-reviewer"])


def test_m1_write_gates_never_auto_approve():
    """红线：任何路径都不得自动放行人工门（旧审计 P0 教训）。"""
    assert rules.AUTO_APPROVE_ALLOWED is False
