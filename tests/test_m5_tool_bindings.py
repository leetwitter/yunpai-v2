"""M5 tool binding & manifest schema tests (Taskbook: tool/skill matrix)."""
import asyncio
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from yunpai_orchestrator import binding as bindings_module
from yunpai_orchestrator.binding import (
    INTENTIONALLY_UNBOUND,
    BindingStatus,
    CatalogView,
    visible_tool_names,
)
from yunpai_orchestrator.registry import build_default_registry
from yunpai_orchestrator.reviewer import rules


IN_SCOPE = {
    "solve_scheduling", "get_m5_schedule", "list_m5_schedules",
    "get_m5_pmc_progress", "get_m5_material_readiness",
    "get_m5_integration_contracts", "replan_m5_schedule",
    "dispatch_m5_schedule", "get_m5_execution_summary",
    "ingest_m5_planning_snapshot", "generate_m5_material_procurement_plan",
    "advise_m5_schedule", "run_m5_intelligent_schedule",
    "search_m5_knowledge", "record_m5_knowledge",
    "prepare_m5_department_message", "get_m5_department_message",
    "get_m5_department_message_delivery",
}
EXCLUDED = {"report_workload", "bind_worker_to_order"}


def test_m5_in_scope_tools_are_bound():
    registry = build_default_registry()
    for name in IN_SCOPE:
        assert name in registry.specs, name
        assert name in registry.handlers, f"{name} 未绑定本地 handler"


def test_m5_excluded_tools_stay_unbound():
    """rows-S6「不搬（设计如此）」2 件：可登记但永久 UNBOUND、不进路由目录。"""
    registry = build_default_registry()
    for name in EXCLUDED:
        assert name in registry.specs
        assert name not in registry.handlers
        assert name in INTENTIONALLY_UNBOUND, f"{name} 必须在 binding.INTENTIONALLY_UNBOUND 名单"
        assert bindings_module.compute_bindings(registry)[name] == BindingStatus.UNBOUND
        assert name not in visible_tool_names(registry), f"{name} 不得进路由目录"
        assert name not in CatalogView(registry).specs, f"{name} 不得进 LLM 候选目录"


def test_m5_write_tools_declare_side_effect_and_gate():
    """改造项①/⑦：6 个写工具的合同字段与 handler 实际副作用对齐。"""
    registry = build_default_registry()
    expected = {
        "solve_scheduling": ("local_write", "apply"),
        "replan_m5_schedule": ("local_write", "apply"),
        "dispatch_m5_schedule": ("external_write", "authorization"),
        "prepare_m5_department_message": ("local_write", "authorization"),
        "record_m5_knowledge": ("local_write", "authorization"),
        "ingest_m5_planning_snapshot": ("local_write", "data"),
    }
    for name, (side_effect, review_gate) in expected.items():
        spec = registry.specs[name]
        assert spec.side_effect == side_effect, name
        assert spec.review_gate == review_gate, name
        # 声明的门必须能归一化（check_contracts W1/W2 口径）
        assert rules.gate_type_for(name, spec), f"{name} 声明的门无法归一化"


def test_m5_rules_cover_write_tools_with_real_findings():
    """改造项⑦：rows-S6 列的 5 个 RULES 覆盖缺口逐条有可命中的 Check。"""
    cases = [
        ("replan_m5_schedule", {"success": True, "data": {"lifecycle_status": "draft"}}, "apply"),
        ("dispatch_m5_schedule", {"success": True, "data": {"status": "pending"}}, "authorization"),
        ("prepare_m5_department_message", {"success": True, "data": {}}, "authorization"),
        ("record_m5_knowledge", {"success": True, "data": {}}, "authorization"),
    ]
    for tool, result, gate in cases:
        findings = rules.evaluate(tool, result)
        assert any(f["gate"] == gate for f in findings), f"{tool} 未开出 {gate} 门"
    # ingest_m5_planning_snapshot 是两条 M5 workflow 的装配步骤：不加新门，
    # 由合同 review_gate=data + BLOCKED_INPUT 规则兜底
    ingest_spec = build_default_registry().specs["ingest_m5_planning_snapshot"]
    assert rules.gate_type_for("ingest_m5_planning_snapshot", ingest_spec) == "blocked_input"
    assert rules.evaluate("ingest_m5_planning_snapshot", {"success": True, "data": {}}) == []
    blocked = rules.evaluate("ingest_m5_planning_snapshot",
                             {"success": False, "code": "BLOCKED_INPUT", "data": {}})
    assert blocked and blocked[0]["gate"] == "blocked_input"
    # 只读工具不得被误开门
    for read_only in ("get_m5_schedule", "list_m5_schedules", "search_m5_knowledge",
                      "get_m5_material_readiness", "get_m5_execution_summary",
                      "advise_m5_schedule", "get_m5_department_message"):
        assert rules.evaluate(read_only, {"success": True, "data": {}}) == [], read_only


def test_m5_output_schema_metadata_is_valid_json_schema():
    registry = build_default_registry()
    for name in IN_SCOPE:
        Draft202012Validator.check_schema(registry.specs[name].output_schema)


def _valid_output_for(name: str) -> dict:
    """Smallest schema-valid output skeleton (used only for pure schema pass)."""
    top = {"success": True, "data": {}, "errors": [], "trace_id": "t"}
    return top


def test_m5_manifest_output_schemas_accept_basic_shapes():
    """Registry-level contract sanity: solve output shape remains schema-valid."""
    registry = build_default_registry()
    validator = Draft202012Validator(registry.specs["solve_scheduling"].output_schema)
    payload = _schema_ok_solve_payload()
    result = asyncio.run(registry.call("solve_scheduling", payload, {"task_id": "T"}))
    validator.validate(result)
    assert result["success"] is True


def _schema_ok_solve_payload():
    return {
        "idempotency_key": "BIND-1",
        "scenario_id": "SC-BIND",
        "scenario_purpose": "production",
        "planning_start": "2026-09-03T08:00:00+08:00",
        "calendar_windows": [
            {"calendar_ref": "CAL-A", "shift_code": "DAY",
             "start_at": "2026-09-03T08:00:00+08:00", "end_at": "2026-09-03T17:00:00+08:00"},
        ],
        "route_code": "ROUTE-P1", "route_version": "approved-v1",
        "route_approval_ref": "APPROVED-001",
        "orders": [{"order_id": "SO-1", "product_id": "P1", "quantity": 2, "uom": "PCS",
                    "due_time": "2026-09-10T17:00:00+08:00"}],
        "resources": [
            {"resource_id": "EQ-A", "name": "设备A", "resource_type": "EQUIPMENT",
             "equipment_type": "press", "capacity_per_hour": 60, "efficiency_factor": 1,
             "calendar_ref": "CAL-A", "status": "available", "capability_codes": ["P"]},
        ],
        "routing_steps": [
            {"product_id": "P1", "operation_id": "OP-10", "sequence": 10,
             "operation_name": "工序10",
             "eligible_resources": [{"resource_id": "EQ-A", "processing_minutes": 5}],
             "standard_minutes": 5, "setup_minutes": 0,
             "required_equipment_codes": ["EQ-A"], "predecessors": [],
             "approval_ref": "APPROVED-001"},
        ],
        "supply_entries": [
            {"order_line_id": "SO-1::L1", "op_code": "OP-10", "readiness": "READY",
             "requirement_ref": "MAT-1", "inventory_snapshot_ref": "INV-1"},
        ],
    }


def test_get_contracts_read_only_returns_catalog():
    registry = build_default_registry()
    result = asyncio.run(registry.call("get_m5_integration_contracts", {}, {"task_id": "T"}))
    Draft202012Validator(registry.specs["get_m5_integration_contracts"].output_schema).validate(result)
    assert result["success"] is True
    assert "mes" in result["data"]["contracts"]
