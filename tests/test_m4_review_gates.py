"""M4 写工具审查门（rows-S5.md §「需补审查（19，系统性 + 2 个 P0）」）。

背景（rows-S5.md:41/89）：V2 对 M4 **没有任何 Gate 路径**——``rules.evaluate`` 只认
``RULES`` 5 条 + ``BLOCKED_INPUT``，``gate_type_for``/``default_gate_for_authorized`` 全仓
零调用；``procurement`` 门类型虽定义（``reviewer/gates.py:14/28``）却无人触发。

本用例集逐条锁定 M4 的 16 个写工具（rows 表中带「审查」的 16 行）在
``reviewer/rules.py`` 都有条目，并覆盖两个 P0：

- **P0** ``approve_m4_purchase_order``：合同派生 ``review_gate=procurement`` 但原无消费路径。
- **P0-5** ``send_m4_purchase_order``：远程禁令（``local_only``/``remote_invocation=forbidden``）
  必须由 registry 真正执行，而不是只写在 manifest 里。
- **P0-1** ``query_m4_material_supply_snapshot``：查询即惰性写快照，不得列入只读白名单。

门型选择理由见 ``reviewer/rules.py`` 的 M4 段落注释（authorization = 执行后人工授权、
approve 不重跑副作用；procurement = 补供应商后可重跑）。
"""
from __future__ import annotations

import pytest

from yunpai_orchestrator.m3_m4_tooling import (
    M4_ADAPTER_TOOL_NAMES,
    M4_READ_ONLY_SKILL_OPERATIONS,
    M4_SKILL_OPERATION_MAP,
)
from yunpai_orchestrator.registry import (
    REMOTE_INVOCATION_FORBIDDEN,
    ToolHTTPError,
    build_default_registry,
    is_remote_adapter,
    mark_remote_adapter,
    remote_invocation_forbidden,
)
from yunpai_orchestrator.reviewer import rules

#: 执行后需人工授权的 M4 写工具（15 个；``import_m4_purchase_suggestions_json``
#: 走「缺供应商 → procurement」条件门，单独断言）。
AUTHORIZATION_GATED: tuple[str, ...] = (
    "generate_m4_purchase_orders",
    "submit_m4_purchase_order_review",
    "approve_m4_purchase_order",
    "request_changes_m4_purchase_order",
    "generate_m4_purchase_inquiry_message",
    "send_m4_purchase_order",
    "create_m4_supplier",
    "update_m4_supplier",
    "create_m4_supplier_reply",
    "parse_m4_supplier_reply",
    "confirm_m4_supplier_reply",
    "confirm_m4_supplier_fact",
    "scan_m4_purchase_alerts",
    "generate_m4_urge_message",
    "query_m4_material_supply_snapshot",
)

#: M4 只读工具：不得开门（防止把读操作也塞进人工门）。
M4_READ_TOOLS: tuple[str, ...] = (
    "list_m4_purchase_suggestions",
    "list_m4_purchase_orders",
    "get_m4_purchase_order",
    "list_m4_suppliers",
    "list_m4_tracking",
    "list_m4_purchase_alerts",
    "list_m4_material_supply_events",
    "get_m4_material_supply_snapshot",
)


# ---------------------------------------------------------------------------
# 16 条逐工具门断言（rows-S5.md 表内 16 个「审查」行）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool", AUTHORIZATION_GATED)
def test_m4_write_tool_opens_authorization_gate(tool):
    """写工具执行成功后必须开人工授权门（approve 决策不重跑副作用）。"""
    findings = rules.evaluate(tool, {"status": "success", "data": {"id": 1}})
    assert [finding["gate"] for finding in findings] == ["authorization"], findings
    assert rules.gate_type_for(tool, None) == "authorization"


@pytest.mark.parametrize("tool", AUTHORIZATION_GATED)
def test_m4_write_tool_failure_does_not_open_gate(tool):
    """失败信封不得被当成功开门（失败另有重试/失败路径）。"""
    assert rules.evaluate(tool, {"status": "failed", "data": {}}) == []


def test_import_m4_purchase_suggestions_json_missing_supplier_opens_procurement_gate():
    """rows-S5.md:67「缺供应商 → procurement Gate」（INT agents.py:478-482 同口径）。"""
    result = {
        "status": "success",
        "data": {"items": [
            {"id": 1, "supplier_name": "SUP-1"},
            {"id": 2, "supplier_name": ""},
        ]},
    }
    findings = rules.evaluate("import_m4_purchase_suggestions_json", result)
    assert [finding["gate"] for finding in findings] == ["procurement"], findings


def test_import_m4_purchase_suggestions_json_with_suppliers_passes():
    """供应商齐备时不拦（否则主链会被无条件 procurement 门反复挂起）。"""
    result = {
        "status": "success",
        "data": {"items": [{"id": 1, "supplier_name": "SUP-1"}, {"id": 2, "supplier_name": "SUP-2"}]},
    }
    assert rules.evaluate("import_m4_purchase_suggestions_json", result) == []


def test_approve_m4_purchase_order_gate_is_p0_regression():
    """P0：``approve_m4_purchase_order`` 原本「派生 procurement 但无任何路径消费」。"""
    assert "approve_m4_purchase_order" in rules.RULES
    findings = rules.evaluate("approve_m4_purchase_order",
                              {"status": "success", "data": {"status": "approved_for_message"}})
    assert findings and findings[0]["gate"] == "authorization"


@pytest.mark.parametrize("tool", M4_READ_TOOLS)
def test_m4_read_tool_opens_no_gate(tool):
    assert rules.evaluate(tool, {"status": "success", "data": {"items": []}}) == []
    assert tool not in rules.RULES


def test_m4_all_write_tools_are_gated_and_never_auto_approved():
    """系统性缺口收口：16 个写工具全部有条目 + 人工门永不被自动放行。"""
    write_tools = set(AUTHORIZATION_GATED) | {"import_m4_purchase_suggestions_json"}
    assert len(write_tools) == 16
    for tool in sorted(write_tools):
        assert tool in rules.RULES, f"{tool} 缺 RULES 条目"
        assert rules.gate_type_for(tool, None) in {"authorization", "procurement"}, tool
    assert rules.AUTO_APPROVE_ALLOWED is False


def test_m4_write_tools_declare_side_effect_and_review_gate():
    """R8 口径：RULES 条目 **+** manifest ``side_effect``/``review_gate`` 双写。

    V2 图只消费 ``RULES``（``rules.evaluate`` 忽略 spec），但契约必须如实声明副作用，
    且与 RULES 门型**一致**（防漂移）。本用例同时是 ``check_contracts.py --strict``
    W1/W2 的本地锁。
    """
    registry = build_default_registry()
    write_tools = set(AUTHORIZATION_GATED) | {"import_m4_purchase_suggestions_json"}
    for tool in sorted(write_tools):
        spec = registry.specs[tool]
        assert spec.side_effect == "local_write", tool
        assert spec.review_gate in {"authorization", "procurement"}, tool
        assert rules.gate_type_for(tool, spec) == spec.review_gate, tool


# ---------------------------------------------------------------------------
# P0-5：send_m4_purchase_order 的远程禁令必须由 registry 真正执行
# ---------------------------------------------------------------------------

def test_send_m4_purchase_order_remote_invocation_is_forbidden():
    """manifest 声明 → registry 永不为其安装 HTTP 适配器（RQ-1 落地）。"""
    registry = build_default_registry()
    spec = registry.specs["send_m4_purchase_order"]
    assert spec.local_only is True
    assert spec.remote_invocation == "forbidden"
    assert spec.remote_invocation_reason
    assert remote_invocation_forbidden(spec) is True
    # 即使调用方显式 bind_http(m4)（overwrite=True），也绝不覆盖本地 handler
    registry.bind_http({"m4": "http://m4.test"}, overwrite=True)
    assert not is_remote_adapter(registry.handlers["send_m4_purchase_order"])
    assert registry.handlers["send_m4_purchase_order"].__module__.endswith("m4_purchase_local")


async def test_registry_refuses_leaked_remote_handler_for_forbidden_tool():
    """防御性断言：即使被外部塞入远程 handler，call() 也必须在发请求前拒绝。"""
    registry = build_default_registry()
    calls: list[str] = []

    async def leaked(payload, context):  # pragma: no cover - 不应被执行
        calls.append("leaked")
        return {}

    registry.handlers["send_m4_purchase_order"] = mark_remote_adapter(leaked)
    with pytest.raises(ToolHTTPError) as excinfo:
        await registry.call("send_m4_purchase_order", {"purchase_order_id": 1},
                            {"task_id": "TASK-1", "tenant_id": "TENANT-1"})
    assert excinfo.value.code == REMOTE_INVOCATION_FORBIDDEN
    assert calls == []


def test_send_m4_purchase_order_removed_from_skill_surface():
    """P0-5：Skill 不得再暴露 send（唯一合法发送出口=询价草稿），HTTP 名单同步摘除。"""
    assert "send" not in M4_SKILL_OPERATION_MAP
    assert "send_m4_purchase_order" not in set(M4_SKILL_OPERATION_MAP.values())
    assert "send_m4_purchase_order" not in M4_ADAPTER_TOOL_NAMES
    assert M4_SKILL_OPERATION_MAP["inquiry"] == "generate_m4_purchase_inquiry_message"


# ---------------------------------------------------------------------------
# P0-1：query_m4_material_supply_snapshot 是写操作，不得列入只读白名单
# ---------------------------------------------------------------------------

def test_query_m4_material_supply_snapshot_is_not_read_only_skill_operation():
    """查询即惰性写快照 → 摘除只读白名单 + 必须有门。"""
    assert "supply" not in M4_READ_ONLY_SKILL_OPERATIONS
    assert "supply_events" in M4_READ_ONLY_SKILL_OPERATIONS  # 真正的只读工具保留
    assert "query_m4_material_supply_snapshot" in rules.RULES
    findings = rules.evaluate("query_m4_material_supply_snapshot",
                              {"status": "success", "data": {"snapshot_id": "current-x"}})
    assert findings and findings[0]["gate"] == "authorization"
