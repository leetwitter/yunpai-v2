"""PMC v2 严格输入反例矩阵（TEST_ACCEPTANCE: 默认值禁止 / v2-only 泄漏 / 缺事实阻断）。

任务书 Task 1 反例固定：
- 不得自动补 08:00-17:00 日历、60/h、效率 1、默认设备或 APPROVED-ROUTE。
- production 请求缺日历/标准工时/产能/效率/审批号/资源绑定/供应事实 -> BLOCKED_INPUT。
- production 请求进入 legacy 分支 -> 阻断（LEGACY_PRODUCTION_BLOCKED）。
- input_hash 使用规范化 JSON；相同 bundle -> 相同 hash。
"""
import pytest

from yunpai_orchestrator.workers import m5_schedule
from yunpai_orchestrator.pmc_v2_adapter import PmcError, run_pmc_v2
from yunpai_orchestrator.pmc_v2_snapshots import PmcError as SnapPmcError


def _base_payload(**overrides):
    payload = {
        "idempotency_key": "STRICT-001",
        "scenario_id": "SC-STRICT",
        "scenario_purpose": "production",
        "planning_start": "2026-09-03T08:00:00+08:00",
        "calendar_windows": [
            {"calendar_ref": "CAL-A", "shift_code": "DAY",
             "start_at": "2026-09-03T08:00:00+08:00", "end_at": "2026-09-03T17:00:00+08:00"},
            {"calendar_ref": "CAL-B", "shift_code": "DAY",
             "start_at": "2026-09-03T08:00:00+08:00", "end_at": "2026-09-03T17:00:00+08:00"},
        ],
        "route_code": "ROUTE-P1",
        "route_version": "approved-v1",
        "route_approval_ref": "APPROVED-001",
        "orders": [{"order_id": "SO-1", "product_id": "P1", "quantity": 2, "uom": "PCS",
                    "due_time": "2026-09-10T17:00:00+08:00"}],
        "resources": [
            {"resource_id": "EQ-A", "resource_type": "EQUIPMENT", "name": "设备A",
             "equipment_type": "press", "capacity_per_hour": 60, "efficiency_factor": 1,
             "calendar_ref": "CAL-A", "status": "available", "capability_codes": ["P"]},
            {"resource_id": "EQ-B", "resource_type": "EQUIPMENT", "name": "设备B",
             "equipment_type": "press", "capacity_per_hour": 60, "efficiency_factor": 1,
             "calendar_ref": "CAL-B", "status": "available", "capability_codes": ["Q"]},
        ],
        "routing_steps": [
            {"product_id": "P1", "operation_id": "OP-10", "sequence": 10,
             "operation_name": "工序10", "standard_minutes": 5, "setup_minutes": 0,
             "required_equipment_codes": ["EQ-A"], "predecessors": [],
             "approval_ref": "APPROVED-001"},
            {"product_id": "P1", "operation_id": "OP-20", "sequence": 20,
             "operation_name": "工序20", "standard_minutes": 5, "setup_minutes": 0,
             "required_equipment_codes": ["EQ-B"], "predecessors": ["OP-10"],
             "approval_ref": "APPROVED-001"},
        ],
        "supply_entries": [
            {"order_line_id": "SO-1::L1", "op_code": "OP-10", "readiness": "READY",
             "requirement_ref": "MAT-1", "inventory_snapshot_ref": "INV-1"},
        ],
    }
    payload.update(overrides)
    return payload


def _raises_blocked(call):
    with pytest.raises((PmcError, SnapPmcError)) as excinfo:
        call()
    return excinfo.value


def test_date_only_calendar_is_rejected_in_production():
    payload = _base_payload(calendar_windows=[{"date": "2026-09-03"}])
    assert _raises_blocked(lambda: run_pmc_v2(payload)).message.startswith("MISSING_CALENDAR_TIME")


def test_calendar_requires_explicit_ref_in_production():
    payload = _base_payload(calendar_windows=[
        {"start_at": "2026-09-03T08:00:00+08:00", "end_at": "2026-09-03T17:00:00+08:00"},
    ])
    assert _raises_blocked(lambda: run_pmc_v2(payload)).message.startswith("MISSING_CALENDAR_REF")


def test_equipment_capacity_missing_blocks_production():
    resource = {"resource_id": "EQ-A", "resource_type": "EQUIPMENT", "name": "设备A",
                "equipment_type": "press", "efficiency_factor": 1,
                "calendar_ref": "CAL-A", "status": "available"}
    payload = _base_payload(resources=[resource])
    assert _raises_blocked(lambda: run_pmc_v2(payload)).message.startswith("MISSING_CAPACITY")


def test_equipment_efficiency_missing_blocks_production():
    resource = {"resource_id": "EQ-A", "resource_type": "EQUIPMENT", "name": "设备A",
                "equipment_type": "press", "capacity_per_hour": 60,
                "calendar_ref": "CAL-A", "status": "available"}
    payload = _base_payload(resources=[resource])
    assert _raises_blocked(lambda: run_pmc_v2(payload)).message.startswith("MISSING_EFFICIENCY")


def test_missing_route_approval_blocks_production():
    payload = _base_payload(route_approval_ref=None)
    for step in payload["routing_steps"]:
        step.pop("approval_ref", None)
    err = _raises_blocked(lambda: run_pmc_v2(payload))
    assert err.message.startswith("MISSING_APPROVAL_REF")


def test_missing_standard_minutes_blocks_production():
    payload = _base_payload()
    payload["routing_steps"][0]["standard_minutes"] = None
    err = _raises_blocked(lambda: run_pmc_v2(payload))
    assert err.message.startswith("MISSING_STANDARD_MINUTES")


def test_production_request_without_v2_facts_is_blocked_not_legacy():
    import asyncio
    payload = {
        "idempotency_key": "LEGACY-PROD",
        "scenario_id": "SC-LEGACY",
        "scenario_purpose": "production",
        "planning_start": "2026-09-03T08:00:00+08:00",
        "orders": [{"order_id": "SO-1", "product_id": "P1", "quantity": 1}],
        "routing_steps": [{"product_id": "P1", "operation_id": "OP-1", "sequence": 1,
                           "processing_minutes": 5, "predecessors": [],
                           "eligible_resources": [{"resource_id": "R-1", "processing_minutes": 5}]}],
        "resources": [{"resource_id": "R-1", "status": "available"}],
    }
    result = asyncio.run(m5_schedule(payload, {"task_id": "T-LEGACY"}))
    assert result["success"] is False
    assert result["errors"][0]["code"] == "LEGACY_PRODUCTION_BLOCKED"


def test_legacy_requires_explicit_preview_marker():
    import asyncio
    payload = {
        "idempotency_key": "PREVIEW-OK",
        "scenario_id": "SC-PREVIEW",
        "scenario_purpose": "production",
        "legacy_preview": True,
        "planning_start": "2026-09-03T08:00:00+08:00",
        "orders": [{"order_id": "SO-1", "product_id": "P1", "quantity": 2}],
        "routing_steps": [{"product_id": "P1", "operation_id": "OP-1", "sequence": 1,
                           "processing_minutes": 5, "predecessors": [],
                           "eligible_resources": [{"resource_id": "R-1", "processing_minutes": 5}]}],
        "resources": [{"resource_id": "R-1", "status": "available"}],
    }
    result = asyncio.run(m5_schedule(payload, {"task_id": "T-PREVIEW"}))
    assert result["success"] is True
    assert result["data"]["schedule"]["plan_version"].startswith("draft-")


def test_missing_supply_readiness_blocks_production():
    payload = _base_payload(supply_entries=[], order_kitting=None)
    err = _raises_blocked(lambda: run_pmc_v2(payload))
    assert err.message.startswith("MISSING_SUPPLY")


def test_input_hash_is_canonical_and_repeatable():
    first = run_pmc_v2(_base_payload())["data"]["input_hash"]
    second = run_pmc_v2(_base_payload())["data"]["input_hash"]
    assert first == second
    assert len(first) == 64
