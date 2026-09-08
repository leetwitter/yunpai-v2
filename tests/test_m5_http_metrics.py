"""M5 HTTP contract mapping + metrics authenticity (Taskbook matrix)."""
import asyncio
import json

import pytest

from yunpai_orchestrator.registry import build_default_registry


def _m5_specs():
    registry = build_default_registry()
    return {name: spec for name, spec in registry.specs.items() if spec.module == "m5"}


def test_http_method_path_mapping_for_query_vs_write_tools():
    specs = _m5_specs()
    assert specs["get_m5_schedule"].method == "GET"
    assert specs["get_m5_schedule"].path == "/api/v1/schedules/{plan_version}"
    assert specs["list_m5_schedules"].method == "GET"
    assert specs["list_m5_schedules"].path == "/api/v1/schedules"
    assert specs["get_m5_pmc_progress"].method == "GET"
    assert specs["get_m5_material_readiness"].method == "GET"
    assert specs["get_m5_material_readiness"].path == "/api/v1/integrations/snapshots/{scenario_id}/readiness"
    assert specs["get_m5_integration_contracts"].method == "GET"
    assert specs["get_m5_execution_summary"].method == "GET"
    assert specs["dispatch_m5_schedule"].method == "POST"
    assert specs["dispatch_m5_schedule"].path == "/api/v1/schedules/{plan_version}/dispatch"
    assert specs["ingest_m5_planning_snapshot"].method == "POST"
    assert specs["replan_m5_schedule"].method == "POST"
    assert specs["replan_m5_schedule"].path.endswith("/replan-from-version")
    assert specs["prepare_m5_department_message"].method == "POST"
    assert specs["record_m5_knowledge"].method == "POST"


def test_http_path_params_substitute_body_placeholders():
    """The registry HTTP adapter substitutes {plan_version}/{scenario_id}."""
    from unittest import mock as _mock
    from yunpai_orchestrator.registry import ToolRegistry
    from yunpai_orchestrator.contracts import ToolSpec
    import httpx

    seen = {}

    class FakeClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, method, url, **kwargs):
            seen["method"] = method
            seen["url"] = url
            return httpx.Response(200, request=httpx.Request(method, url),
                                  json={"success": True, "data": {}, "errors": [], "trace_id": "t"})

    registry = ToolRegistry()
    registry.register(ToolSpec(
        "get_m5_schedule", "m5", "查询排程",
        {"type": "object", "properties": {"plan_version": {"type": "string"}}},
        {"type": "object"},
        base_url="http://m5-api:8000", method="GET", path="/api/v1/schedules/{plan_version}",
    ))
    monkey = _mock.patch("httpx.AsyncClient", FakeClient)
    with monkey:
        registry.bind_http({"m5": "http://m5-api:8000"})
        asyncio.run(registry.call("get_m5_schedule", {"plan_version": "plan-v9"}, {"task_id": "T"}))
    assert seen["url"] == "http://m5-api:8000/api/v1/schedules/plan-v9"
    assert seen["method"] == "GET"


def test_metrics_are_null_or_present_not_fabricated():
    """v2 schedule metrics must stay truthful when data is absent."""
    from yunpai_orchestrator.pmc_v2_adapter import run_pmc_v2
    from yunpai_orchestrator.pmc_v2_snapshots import PmcError
    payload = {
        "idempotency_key": "MET-1", "scenario_id": "SC-MET",
        "scenario_purpose": "production",
        "planning_start": "2026-09-03T08:00:00+08:00",
        "calendar_windows": [{"calendar_ref": "CAL-A", "shift_code": "DAY",
                              "start_at": "2026-09-03T08:00:00+08:00",
                              "end_at": "2026-09-03T17:00:00+08:00"}],
        "route_code": "R-P1", "route_version": "v1", "route_approval_ref": "AP-1",
        "orders": [{"order_id": "SO-1", "product_id": "P1", "quantity": 2, "uom": "PCS",
                    "due_time": "2026-09-10T17:00:00+08:00"}],
        "resources": [{"resource_id": "EQ-A", "name": "A", "resource_type": "EQUIPMENT",
                       "equipment_type": "press", "capacity_per_hour": 60, "efficiency_factor": 1,
                       "calendar_ref": "CAL-A", "status": "available"}],
        "routing_steps": [{"product_id": "P1", "operation_id": "OP-1", "sequence": 1,
                           "operation_name": "op1",
                           "eligible_resources": [{"resource_id": "EQ-A", "processing_minutes": 5}],
                           "standard_minutes": 5, "required_equipment_codes": ["EQ-A"],
                           "predecessors": [], "approval_ref": "AP-1"}],
        "supply_entries": [{"order_line_id": "SO-1::L1", "op_code": "OP-1", "readiness": "READY",
                            "requirement_ref": "M", "inventory_snapshot_ref": "I"}],
    }
    result = run_pmc_v2(payload)
    metrics = result["data"]["schedule"]["metrics"]
    assert metrics["operation_count"] == 1
    # tardiness/on-time are solver-quality metrics; when the engine has no
    # due-target optimization the fields must not be silently invented.
    assert "total_tardiness_minutes" not in metrics or metrics["total_tardiness_minutes"] is None
    assert "on_time_rate" not in metrics or metrics["on_time_rate"] is None


def test_dispatch_execution_pending_and_replayable_in_registry():
    """dispatch through the registry stays pending (no MES sender claim)."""
    import tempfile
    from pathlib import Path
    from yunpai_orchestrator import m5_tools as mt
    from yunpai_orchestrator.m5_repository import M5Repository
    from yunpai_orchestrator.pmc_v2_adapter import run_pmc_v2

    base = _solve_payload("SC-DISP")
    db = Path(tempfile.mkdtemp()) / "m5.sqlite"
    ctx = {"task_id": "T-D", "tenant_id": "t", "m5_db_path": str(db)}
    repo = M5Repository(db)
    solved = run_pmc_v2(base)
    digest = solved["data"]["input_hash"]
    version = f"plan-{digest[:10]}"
    repo.save_plan(plan_version=version, scenario_id="SC-DISP", tenant_id="t", task_id="T-D",
                   lifecycle_status="draft", parent_plan_version=None, input_hash=digest,
                   solver_hash="s", algorithm_version="a", scenario_purpose="production",
                   validation_report=solved["data"]["validator"],
                   bundle=solved["data"]["input_package"], schedule=solved["data"]["schedule"])
    repo.transition(version, "approved", gate="review", actor="zhb")
    repo.transition(version, "released", gate="release", actor="zhb")
    repo.set_head("SC-DISP", version)
    d1 = asyncio.run(mt.m5_dispatch_schedule(
        {"plan_version": version, "idempotency_key": "DD-1",
         "operation_keys": ["SO-1:OP-1"]}, ctx))
    d2 = asyncio.run(mt.m5_dispatch_schedule(
        {"plan_version": version, "idempotency_key": "DD-1",
         "operation_keys": ["SO-1:OP-1"]}, ctx))
    assert d1["data"]["status"] == "pending"
    assert d1["data"]["total_count"] == 1 and d1["data"]["acknowledged_count"] == 0
    assert d2["data"]["replayed"] is True


def _solve_payload(scenario_id):
    return {
        "idempotency_key": f"K-{scenario_id}", "scenario_id": scenario_id,
        "scenario_purpose": "production",
        "planning_start": "2026-09-03T08:00:00+08:00",
        "calendar_windows": [{"calendar_ref": "CAL-A", "shift_code": "DAY",
                              "start_at": "2026-09-03T08:00:00+08:00",
                              "end_at": "2026-09-03T17:00:00+08:00"}],
        "route_code": "R-P1", "route_version": "v1", "route_approval_ref": "AP-1",
        "orders": [{"order_id": "SO-1", "product_id": "P1", "quantity": 2, "uom": "PCS",
                    "due_time": "2026-09-10T17:00:00+08:00"}],
        "resources": [{"resource_id": "EQ-A", "name": "A", "resource_type": "EQUIPMENT",
                       "equipment_type": "press", "capacity_per_hour": 60, "efficiency_factor": 1,
                       "calendar_ref": "CAL-A", "status": "available",
                       "capability_codes": ["P"]}],
        "routing_steps": [{"product_id": "P1", "operation_id": "OP-1", "sequence": 1,
                           "operation_name": "op1",
                           "eligible_resources": [{"resource_id": "EQ-A", "processing_minutes": 5}],
                           "standard_minutes": 5, "required_equipment_codes": ["EQ-A"],
                           "predecessors": [], "approval_ref": "AP-1"}],
        "supply_entries": [{"order_line_id": "SO-1::L1", "op_code": "OP-1", "readiness": "READY",
                            "requirement_ref": "M", "inventory_snapshot_ref": "I"}],
    }
