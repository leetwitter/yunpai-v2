from yunpai_orchestrator.m0_backend import M0Store


def test_m0_backend_publishes_and_reads_back(tmp_path):
    store = M0Store(tmp_path / "m0.sqlite")
    batch = store.ingest(
        [{"filename": "order.json", "entity_type": "order", "order_id": "SO-001", "product_code": "W-H909"}],
        tenant_id="tenant-main",
        task_id="task-1",
    )
    result = store.publish(
        batch["batch_id"],
        actor="reviewer-1",
        human_override=True,
        reason="人工确认原始订单有效",
    )
    assert result["status"] == "published"
    assert result["approved_candidates"] == 1
    assert result["ledger_count"] == 1
    assert result["outbox_count"] == 1
    assert result["entities"][0]["canonical_key"] == "W-H909"


def test_list_entities_reads_back_approved_resources_by_type(tmp_path):
    store = M0Store(tmp_path / "m0.sqlite")
    records = [
        {"filename": "machine.xlsx", "entity_type": "equipment_master",
         "equipment_code": "EQ-01", "equipment_type": "冲压机",
         "capacity_per_hour": "120", "efficiency_factor": "0.9",
         "status": "available", "calendar_ref": "CAL-A", "capability_codes": ["stamping"]},
        {"filename": "station.xlsx", "entity_type": "station_master",
         "station_code": "ST-01", "work_center_code": "WC-A",
         "parallel_slots": 1, "status": "available", "calendar_ref": "CAL-A"},
        {"filename": "worker.xlsx", "entity_type": "worker_master",
         "person_code": "P-01", "skill_codes": ["stamping"],
         "calendar_ref": "CAL-A", "status": "available"},
    ]
    batch = store.ingest(records, tenant_id="tenant-main", task_id="task-2")
    result = store.publish(batch["batch_id"], actor="reviewer-1", reason="资源事实经人工审批")
    assert result["status"] == "published"

    equipment = store.list_entities("equipment_master", tenant_id="tenant-main")
    assert equipment["count"] == 1
    assert equipment["entities"][0]["canonical_key"] == "EQ-01"
    assert equipment["entities"][0]["payload_json"]["capacity_per_hour"] == "120"

    stations = store.list_entities("station_master", tenant_id="tenant-main")
    assert stations["count"] == 1
    assert stations["entities"][0]["payload_json"]["station_code"] == "ST-01"

    # 未审批或其它 entity_type 不应串读
    assert store.list_entities("production_calendar", tenant_id="tenant-main")["count"] == 0
    assert store.list_entities("equipment_master", tenant_id="other-tenant")["count"] == 0
