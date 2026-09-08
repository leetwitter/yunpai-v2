"""PMC v2 求解器跨窗口放置（splitting）回归测试。

修复前：`_place_processing` 要求单个工序的 (setup + processing) 完整落在
单一连续工作窗口内，任何超过最长窗口（如 4 小时班次）的工序会报
NO_FEASIBLE_WINDOW。修复后：processing 分钟可跨多个工作窗口累积（午休/跨天
视为非工作时间，仅延长墙钟跨度）。
"""
from datetime import datetime

import pytest

from yunpai_orchestrator.pmc_v2_adapter import run_pmc_v2
from yunpai_orchestrator.pmc_v2_scheduler import RunContext, _place_processing
from yunpai_orchestrator.pmc_v2_snapshots import to_minutes, parse_dt


def _calendar(days=3):
    windows = []
    for d in range(1, days + 1):
        date = f"2026-09-0{d}"
        windows += [
            {"calendar_ref": "CAL-A", "shift_code": "DAY",
             "start_at": f"{date}T08:00:00+08:00", "end_at": f"{date}T12:00:00+08:00"},
            {"calendar_ref": "CAL-A", "shift_code": "DAY2",
             "start_at": f"{date}T13:30:00+08:00", "end_at": f"{date}T17:30:00+08:00"},
        ]
    return windows


def _manual_payload(processing_minutes):
    return {
        "idempotency_key": "SPAN-MANUAL",
        "scenario_id": "SC-SPAN",
        "scenario_purpose": "production",
        "planning_start": "2026-09-01T08:00:00+08:00",
        "calendar_windows": _calendar(),
        "route_code": "ROUTE-P1",
        "route_version": "approved-v1",
        "route_approval_ref": "APPROVED-001",
        "orders": [{"order_id": "SO-1", "product_id": "P1", "quantity": 1, "uom": "PCS",
                    "due_time": "2026-09-10T17:00:00+08:00"}],
        "resources": [
            {"resource_id": "P-1", "resource_type": "PERSON", "calendar_ref": "CAL-A",
             "status": "available", "max_parallel_tasks": 1, "skill_codes": ["repair"]},
            {"resource_id": "ST-1", "resource_type": "STATION", "calendar_ref": "CAL-A",
             "status": "available", "parallel_slots": 1},
        ],
        "routing_steps": [{
            "product_id": "P1", "operation_id": "OP-1", "sequence": 1,
            "operation_name": "手动维修", "standard_minutes": processing_minutes,
            "setup_minutes": 0,
            "required_person_codes": ["P-1"], "required_station_codes": ["ST-1"],
        }],
        "supply_entries": [
            {"order_line_id": "SO-1::L1", "op_code": "OP-1", "readiness": "READY",
             "requirement_ref": "MAT-1", "inventory_snapshot_ref": "INV-1"},
        ],
    }


def test_manual_operation_spans_multiple_windows():
    # 600 工作分钟 > 单个窗口(240 分钟)，但跨窗可容纳。
    result = run_pmc_v2(_manual_payload(600))
    assert result["success"] is True, result.get("errors")
    op = result["data"]["schedule"]["operations"][0]
    start = datetime.fromisoformat(op["plan_start"])
    end = datetime.fromisoformat(op["plan_end"])
    wall_span = (end - start).total_seconds() / 60
    assert op["processing_minutes"] == 600
    assert wall_span > 600, "跨窗口的墙钟跨度必须包含午休/跨天间隙"


def test_manual_operation_longer_than_any_single_window_is_feasible():
    # 之前版本会在这里 NO_FEASIBLE_WINDOW（800 分钟 > 240 分钟窗口）。
    result = run_pmc_v2(_manual_payload(800))
    assert result["success"] is True, result.get("errors")
    assert result["data"]["schedule"]["operations"][0]["processing_minutes"] == 800


def test_operation_still_blocked_when_calendar_exhausted():
    # 3 天 × 8 小时 = 1440 分钟 < 2000 分钟，应失败关闭。
    with pytest.raises(Exception) as excinfo:
        run_pmc_v2(_manual_payload(2000))
    assert "NO_FEASIBLE_WINDOW" in str(excinfo.value)


def test_equipment_operation_spans_windows():
    # 设备工序：qty=800, std=1min/pc, cap=120/h, eff=1 -> 400 工作分钟 > 240，
    # 跨两个工作窗口。
    payload = _manual_payload(1)
    payload["orders"][0]["quantity"] = 800
    payload["routing_steps"][0]["required_equipment_codes"] = ["EQ-1"]
    payload["resources"].append(
        {"resource_id": "EQ-1", "resource_type": "EQUIPMENT", "equipment_type": "press",
         "calendar_ref": "CAL-A", "status": "available", "capability_codes": ["stamping"],
         "capacity_per_hour": 120, "efficiency_factor": 1},
    )
    result = run_pmc_v2(payload)
    assert result["success"] is True, result.get("errors")
    op = result["data"]["schedule"]["operations"][0]
    assert op["equipment_code"] == "EQ-1"
    assert op["processing_minutes"] == 400


def test_equipment_anchor_in_lunch_gap_starts_at_next_window():
    # 锚点落在午休间隙（12:30）时，处理不得从间隙开始，而应从下一窗口 13:30 开始。
    payload = _manual_payload(1)
    payload["orders"][0]["quantity"] = 100
    payload["routing_steps"][0]["required_equipment_codes"] = ["EQ-1"]
    payload["resources"].append(
        {"resource_id": "EQ-1", "resource_type": "EQUIPMENT", "equipment_type": "press",
         "calendar_ref": "CAL-A", "status": "available", "capability_codes": ["stamping"],
         "capacity_per_hour": 120, "efficiency_factor": 1},
    )
    from yunpai_orchestrator.pmc_v2_adapter import build_bundle
    ctx = RunContext(build_bundle(payload))
    combo = {
        "equipment": {"equipment_code": "EQ-1", "status": "ACTIVE",
                      "capacity_per_hour": "120", "efficiency_factor": "1.0",
                      "calendar_ref": "CAL-A"},
        "person": None, "station": None, "tooling": [],
    }
    anchor = to_minutes(parse_dt("2026-09-01T12:30:00+08:00"))
    placed = _place_processing(ctx, combo, anchor, 0, 100)
    assert placed is not None
    start, _end = placed
    assert start >= to_minutes(parse_dt("2026-09-01T13:30:00+08:00")), \
        "processing must not start inside the lunch gap"
