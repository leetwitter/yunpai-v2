"""M0→M5 桥接集成（V2 版）：六类 snapshot 从 M0 canonical 读回资源/日历事实。

rows-S6 需补测试（INT `test_m0_to_m5_e2e` 的 V2 版）+ 父会话 M0 读口裁决：
M5 资源/日历/工艺路线读 M0 canonical 一律走 `m0_backend.M0Store.list_entities`
（路径取 `YUNPAI_M0_DB`），**禁止**直连 sqlite3 读 `canonical_entities`；
V2 没有 INT 的 `fact_gateway`，读取处把 `payload.attributes` 并入顶层（顶层优先）。
"""
import base64
import json

from yunpai_orchestrator.m0_backend import M0Store
from yunpai_orchestrator.orchestration_bridge import _read_m0_entities, bridge_payload


RESOURCE_RECORDS = [
    {"entity_type": "equipment_master", "equipment_code": "EQ-1", "equipment_type": "冲压机",
     "capability_codes": ["stamping"], "capacity_per_hour": "120", "efficiency_factor": "0.9",
     "status": "available", "calendar_ref": "CAL-1"},
    {"entity_type": "station_master", "station_code": "ST-1", "work_center_code": "WC-1",
     "parallel_slots": 1, "status": "available", "calendar_ref": "CAL-1"},
    {"entity_type": "worker_master", "person_code": "P-1", "skill_codes": ["stamping"],
     "qualified_operation_codes": ["OP-1"], "max_parallel_tasks": 1,
     "status": "available", "calendar_ref": "CAL-1"},
    {"entity_type": "tooling_master", "tooling_code": "FIX-1", "tooling_type": "fixture",
     "capability_codes": ["stamping"], "quantity_available": 1, "status": "available",
     "calendar_ref": "CAL-1"},
    {"entity_type": "production_calendar", "calendar_ref": "CAL-1",
     "working_intervals": [{"calendar_ref": "CAL-1", "shift_code": "DAY",
                            "start_at": "2026-09-05T08:00:00+08:00",
                            "end_at": "2026-09-05T17:00:00+08:00"}],
     "unavailability": []},
]


def _publish_m0(tmp_path, records, tenant_id="tenant-main"):
    store = M0Store(tmp_path / "m0.sqlite")
    batch = store.ingest(records, tenant_id=tenant_id, task_id="t")
    store.publish(batch["batch_id"], actor="reviewer", reason="审批资源")
    return store


def _state():
    doc = {"order_id": "SO-1", "product_code": "P-1", "quantity": 2, "due_date": "2026-09-10"}
    encoded = base64.b64encode(json.dumps(doc).encode()).decode()
    return {
        "task_id": "task-1", "tenant_id": "tenant-main", "site_id": "default",
        "request": {
            "workflow": "m1_m5_document_to_plan", "scenario_purpose": "production",
            "document": {"_encoded": encoded, **doc},
            "bom_lines": [{"material_code": "MAT-1", "quantity_per": 3}],
            "inventory": [{"material_code": "MAT-1", "warehouse": "WH-1", "lot_no": "LOT-1",
                           "available_qty": 6, "locked_qty": 0, "qc_status": "released",
                           "received_at": "2026-09-01"}],
            "routing_steps": [{"operation_id": "OP-1", "sequence": 1, "standard_minutes": 5,
                               "required_equipment_codes": ["EQ-1"],
                               "required_person_codes": ["P-1"],
                               "required_station_codes": ["ST-1"]}],
            "route_approval_ref": "APPROVED-SIM-001",
            "route_code": "R-P1",
            "route_version": "v1",
            "supply_entries": [{"order_line_id": "SO-1::L1", "readiness": "READY",
                                "requirement_ref": "MAT-1", "inventory_snapshot_ref": "INV-1"}],
            "setup_matrix": {"OP-1": {"OP-1": 0}},
        },
        "outputs": {
            "ingest_document": {"data": {"order": {"order_id": "SO-1", "product_code": "P-1",
                                                   "quantity": 2, "due_date": "2026-09-10"}}},
            "run_bom_sop_workflow": {"data": {
                "bom_generation": {"bom_lines": [{"material_code": "MAT-1", "quantity_per": 3}],
                                   "approval_status": "approved"},
                "sop_generation": {"route_steps": [{"operation_id": "OP-1", "sequence": 1,
                                                    "standard_minutes": 5,
                                                    "required_equipment_codes": ["EQ-1"],
                                                    "required_person_codes": ["P-1"],
                                                    "required_station_codes": ["ST-1"]}],
                                   "approval_status": "approved"},
            }},
            "run_m3_procurement_requirements": {"data": {"procurement_plan_id": "plan-1",
                                                         "order_id": "SO-1", "shortage_lines": []}},
        },
    }


def test_bridge_assembles_six_class_bundle_with_m0_resource_facts(tmp_path, monkeypatch):
    _publish_m0(tmp_path, RESOURCE_RECORDS)
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)

    payload = bridge_payload(_state(), "ingest_m5_planning_snapshot")
    assert payload.get("success") is not False, payload
    bundle = payload["pmc_v2_bundle"]

    # 六类 snapshot 全部由 bridge 组装（verify_bundle 不再报缺失）
    for kind in ("order_snapshots", "routes", "resource_snapshot", "calendar_snapshot",
                 "supply_snapshot", "constraint_snapshot"):
        assert kind in bundle, f"{kind} 缺失"

    # 资源/日历事实来自 M0 canonical 读回（M0Store.list_entities），而非 request 直传
    equipment = bundle["resource_snapshot"]["equipment"]
    assert equipment[0]["equipment_code"] == "EQ-1"
    assert equipment[0]["capacity_per_hour"] == "120"
    assert equipment[0]["calendar_ref"] == "CAL-1"
    assert bundle["resource_snapshot"]["stations"][0]["station_code"] == "ST-1"
    assert bundle["resource_snapshot"]["persons"][0]["person_code"] == "P-1"
    assert bundle["resource_snapshot"]["tooling"][0]["tooling_code"] == "FIX-1"
    assert bundle["calendar_snapshot"]["working_intervals"][0]["calendar_ref"] == "CAL-1"


def test_bridge_fails_closed_when_m0_has_no_resource_facts(tmp_path, monkeypatch):
    # 库不存在（也未被创建）→ 读口返回空 → 缺资源/日历事实必须失败关闭
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "missing.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)

    payload = bridge_payload(_state(), "ingest_m5_planning_snapshot")
    assert payload["success"] is False
    assert payload["code"] == "BLOCKED_INPUT"
    missing = payload["data"]["missing_fields"]
    assert any("resource_snapshot" in field or "calendar_snapshot" in field for field in missing)
    assert not (tmp_path / "missing.sqlite").exists(), "读不到时不得创建空库"


def test_read_m0_entities_merges_attributes_payload(tmp_path, monkeypatch):
    """父会话裁决②：payload.attributes 并入顶层（顶层优先），兼容 SOP 文档形状。"""
    _publish_m0(tmp_path, [{
        "entity_type": "document", "doc_id": "DOC-1", "product_code": "P-1",
        "attributes": {"route_steps": [{"operation_id": "OP-1", "sequence": 1}],
                       "product_codes": ["P-1"], "product_code": "SHOULD-NOT-WIN"},
    }])
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)

    rows = _read_m0_entities({"tenant_id": "tenant-main"}, "document")
    assert len(rows) == 1
    row = rows[0]
    assert row["route_steps"] == [{"operation_id": "OP-1", "sequence": 1}]
    assert row["product_codes"] == ["P-1"]
    # 顶层优先：attributes 里的同名字段不得覆盖顶层事实
    assert row["product_code"] == "P-1"
    assert row["canonical_key"] == "P-1"


def test_read_m0_entities_isolates_tenants(tmp_path, monkeypatch):
    """读口按 tenant 隔离：别的租户的 canonical 事实不得进 M5 快照。"""
    _publish_m0(tmp_path, RESOURCE_RECORDS, tenant_id="tenant-other")
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0.sqlite"))
    monkeypatch.delenv("M0_URL", raising=False)
    assert _read_m0_entities({"tenant_id": "tenant-main"}, "equipment_master") == []
    assert len(_read_m0_entities({"tenant_id": "tenant-other"}, "equipment_master")) == 1
