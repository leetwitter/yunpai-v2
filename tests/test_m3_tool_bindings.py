"""M3 17 件工具：绑定态 / ``required_headers`` / 声明源一致性（rows-S4 §B 逐行口径）。

对应 PROMPT-M3「13 个保留HTTP：不改 handler，确认 V2 manifest 的绑定态与
``required_headers``，各补 1 条绑定断言」+「不搬/废弃进名单并补断言」。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from yunpai_orchestrator.binding import (
    DEFAULT_ROUTER_HIDDEN,
    DEPRECATED_TOOLS,
    ORCHESTRATION_INTERNAL,
    BindingStatus,
    compute_bindings,
    visible_tool_names,
)
from yunpai_orchestrator.m3_m4_tooling import (
    M3_ADAPTER_TOOL_NAMES,
    M3_LEGACY_DECISIONS,
    M3_LEGACY_TOOL_NAMES,
    M3_NO_BUSINESS_DATA_TOOLS,
    M3_READ_ONLY_SKILL_OPERATIONS,
    M3_SKILL_OPERATION_MAP,
    M3_SKILL_SURFACE_TOOL_NAMES,
    M3_TOOL_DECLARATIONS,
    M3_TOOL_NAMES,
)
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.workers import HANDLERS

REPO_ROOT = Path(__file__).resolve().parents[1]
_M3_MANIFEST = REPO_ROOT / "registry-manifests" / "m3.json"

#: rows-S4 判「保留HTTP」的 13 件：V2 侧不得登记本地 handler（登记会遮蔽 HTTP 绑定）。
RETAINED_HTTP: tuple[str, ...] = (
    "list_m3_orders",
    "get_m3_order",
    "get_m3_procurement_plan",
    "get_persisted_m3_plan",
    "get_material_readiness",
    "get_pr_po_drafts",
    "get_m3_approval_tasks",
    "approve_m3_task",
    "reject_m3_task",
    "request_change_m3_task",
    "approve_to_send_m3_task",
    "get_m3_m4_handoffs",
    "export_m3_procurement_suggestions",
)

#: 旧审批四件：书二 §7.3 ○（默认不进路由目录）。
APPROVAL_TOOLS: tuple[str, ...] = (
    "approve_m3_task",
    "reject_m3_task",
    "request_change_m3_task",
    "approve_to_send_m3_task",
)

#: 本分片迁入的本地实现（改造后搬）。
LOCAL_MIGRATED: tuple[str, ...] = (
    "get_material_readiness_snapshot",
    "run_m3_procurement_requirements",
)


def _manifest_tools() -> dict:
    return {tool["name"]: tool for tool in json.loads(_M3_MANIFEST.read_text(encoding="utf-8"))["tools"]}


@pytest.fixture(scope="module")
def registry():
    return build_default_registry()


def test_m3_declarations_match_manifest_exactly():
    tools = _manifest_tools()
    assert len(tools) == 17
    assert M3_TOOL_NAMES == tuple(tools)          # 顺序也一致
    assert set(M3_TOOL_DECLARATIONS) == set(tools)


def test_legacy_flag_matches_manifest_description():
    for name, tool in _manifest_tools().items():
        is_legacy = str(tool.get("description") or "").startswith("[LEGACY]")
        assert (name in M3_LEGACY_TOOL_NAMES) is is_legacy, name


def test_adapter_and_skill_surface_agree():
    assert M3_ADAPTER_TOOL_NAMES == M3_SKILL_SURFACE_TOOL_NAMES
    assert set(M3_SKILL_OPERATION_MAP.values()) == set(M3_ADAPTER_TOOL_NAMES)
    assert "run_m3_procurement_requirements" in M3_ADAPTER_TOOL_NAMES
    assert set(M3_SKILL_OPERATION_MAP) == {
        operation
        for declaration in M3_TOOL_DECLARATIONS.values()
        for operation in declaration["skill_operations"]
    }


def test_read_only_operations_exclude_no_business_data_tools():
    targets = {M3_SKILL_OPERATION_MAP[op] for op in M3_READ_ONLY_SKILL_OPERATIONS}
    assert not (targets & M3_NO_BUSINESS_DATA_TOOLS)
    assert M3_READ_ONLY_SKILL_OPERATIONS == frozenset({"orders", "order", "readiness_snapshot"})


def test_legacy_decisions_cover_exactly_the_legacy_tools():
    assert len(M3_LEGACY_TOOL_NAMES) == 14
    assert set(M3_LEGACY_DECISIONS) == set(M3_LEGACY_TOOL_NAMES)
    assert len(M3_NO_BUSINESS_DATA_TOOLS) == 12
    assert M3_NO_BUSINESS_DATA_TOOLS <= M3_LEGACY_TOOL_NAMES


def test_retained_http_tools_stay_http_bound_and_unshadowed(registry):
    """13 件保留HTTP：绑定态 BOUND_HTTP、有 HTTP handler、无本地 handler 遮蔽。"""
    bindings = compute_bindings(registry)
    tools = _manifest_tools()
    for name in RETAINED_HTTP:
        assert name not in HANDLERS, f"{name} 不得登记本地 handler（会遮蔽 HTTP 绑定）"
        assert name in registry.handlers, f"{name} 应已绑定 HTTP handler"
        assert bindings[name] == BindingStatus.BOUND_HTTP, name
        spec = registry.specs[name]
        assert spec.method == tools[name]["http"]["method"]
        assert spec.path == tools[name]["http"]["path"]


def test_m3_required_headers_declared_state(registry):
    """确认 V2 manifest 未声明 required_headers（M3 侧鉴权走 M3_AUTHORIZATION/M3_API_KEY + 通用头）。"""
    tools = _manifest_tools()
    for name in M3_TOOL_NAMES:
        assert "required_headers" not in tools[name]["http"], name
        assert registry.specs[name].required_headers == (), name


def test_approval_tools_hidden_from_router_catalog(registry):
    """书二 §7.3 ○：审批四件仍是已绑定，但默认不进路由目录（LLM 不得提案写动作）。"""
    bindings = compute_bindings(registry)
    visible = set(visible_tool_names(registry))
    tools = _manifest_tools()
    for name in APPROVAL_TOOLS:
        assert name in DEFAULT_ROUTER_HIDDEN
        assert name not in visible
        assert bindings[name] == BindingStatus.BOUND_HTTP      # 绑定态照实报
        assert registry.specs[name].side_effect == "external_write"
        assert registry.specs[name].review_gate == "authorization"
        assert tools[name]["side_effect"] == "external_write"
        assert tools[name]["review_gate"] == "authorization"


def test_local_migrated_tools_are_bound_local(registry):
    bindings = compute_bindings(registry)
    for name in LOCAL_MIGRATED:
        assert name in HANDLERS
        assert bindings[name] == BindingStatus.BOUND_LOCAL, name
    assert bindings["run_mrp_procurement_plan"] == BindingStatus.UNBOUND
    assert "run_mrp_procurement_plan" in DEPRECATED_TOOLS
    assert "run_mrp_procurement_plan" not in visible_tool_names(registry)


def test_receive_endpoint_is_orchestrator_only(registry):
    name = "receive_m3_material_demand"
    assert name in ORCHESTRATION_INTERNAL
    assert M3_TOOL_DECLARATIONS[name]["surface"] == "orchestrator"
    assert M3_TOOL_DECLARATIONS[name]["skill_operations"] == ()
    assert name not in M3_ADAPTER_TOOL_NAMES
    assert name not in set(M3_SKILL_OPERATION_MAP.values())
    assert name not in HANDLERS
    assert name not in registry.handlers
    assert compute_bindings(registry)[name] == BindingStatus.UNBOUND
    assert name not in visible_tool_names(registry)


def test_legacy_decision_block_reads_single_source():
    """``m3_local._legacy_block`` 消费 ``M3_LEGACY_DECISIONS``（单一声明源）。"""
    from yunpai_orchestrator.m3_local import _legacy_block

    deprecated = _legacy_block("get_m3_procurement_plan")
    assert deprecated["deprecated"] is True
    assert deprecated["deprecation"]["replacement"] == "run_m3_procurement_requirements"
    assert deprecated["deprecation"]["failure_code"] == "LEGACY_UNAVAILABLE"
    kept = _legacy_block("list_m3_orders")
    assert kept["deprecated"] is False
    assert kept["deprecation"]["status"] == "keep"
    assert _legacy_block("not_a_tool") == {}


def test_manifest_query_fields_are_the_single_source(registry):
    """R4-REQ-5：tenant_id 走 query 的 6 件由 manifest 声明，registry 读取该字段。"""
    from yunpai_orchestrator.registry import _http_query_fields

    tools = _manifest_tools()
    declared = {
        "get_persisted_m3_plan", "get_pr_po_drafts",
        "approve_m3_task", "reject_m3_task", "request_change_m3_task",
        "approve_to_send_m3_task",
    }
    for name, tool in tools.items():
        query_fields = tool["http"].get("query_fields")
        if name in declared:
            assert query_fields == ["tenant_id"], name
            assert registry.specs[name].query_fields == ("tenant_id",)
            assert _http_query_fields(registry.specs[name]) == frozenset({"tenant_id"})
        else:
            assert not query_fields, name
