"""Task 2 through the registry: solve_scheduling persists draft + idempotency.

These tests enable the repository only via ctx m5_db_path; the default
stateless registry path (graph / preview flows) stays unchanged.
"""
import asyncio

import pytest

from yunpai_orchestrator.registry import build_default_registry


def _payload(**overrides):
    base = {
        "idempotency_key": "IDEM-1",
        "scenario_id": "SC-IDEM",
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
    base.update(overrides)
    return base


async def _solve(payload, ctx):
    registry = build_default_registry()
    return await registry.call("solve_scheduling", payload, ctx)


@pytest.mark.asyncio
async def test_solve_with_repository_persists_snapshots_and_draft(tmp_path):
    ctx = {"task_id": "T-1", "tenant_id": "t", "m5_db_path": str(tmp_path / "m5.sqlite")}
    payload = _payload()
    result = await _solve(payload, ctx)
    assert result["success"] is True
    assert result["data"]["plan_version"].startswith("plan-SC-IDEM-")
    from yunpai_orchestrator.m5_repository import M5Repository
    repo = M5Repository(ctx["m5_db_path"])
    bundle = repo.load_bundle("SC-IDEM")
    assert bundle is not None and bundle["routes"]["P1"]["operations"][0]["op_code"] == "OP-10"
    plan = repo.get_plan(result["data"]["plan_version"])
    assert plan["lifecycle_status"] == "draft"


@pytest.mark.asyncio
async def test_solve_same_idempotency_same_input_replays(tmp_path):
    ctx = {"task_id": "T-1", "tenant_id": "t", "m5_db_path": str(tmp_path / "m5.sqlite")}
    first = await _solve(_payload(), ctx)
    second = await _solve(_payload(), ctx)
    assert second["success"] is True
    assert second["data"]["plan_version"] == first["data"]["plan_version"]
    assert second["data"]["replayed"] is True


@pytest.mark.asyncio
async def test_solve_same_idempotency_different_input_conflicts(tmp_path):
    ctx = {"task_id": "T-1", "tenant_id": "t", "m5_db_path": str(tmp_path / "m5.sqlite")}
    await _solve(_payload(), ctx)
    changed = _payload()
    changed["orders"][0]["quantity"] = 9
    result = await _solve(changed, ctx)
    assert result["success"] is False
    assert result["errors"][0]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.asyncio
async def test_stateless_solve_without_repository_unchanged():
    ctx = {"task_id": "T-1", "tenant_id": "t"}
    result = await _solve(_payload(), ctx)
    assert result["success"] is True
    assert "plan_version" not in result["data"] or result["data"].get("plan_version", "").startswith("wip-v2-")
