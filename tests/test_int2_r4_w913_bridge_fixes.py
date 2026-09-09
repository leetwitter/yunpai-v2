"""INT2 第四轮（K1–K6）：W913 workflow 路径的 4+1 类装配卡点回归。

对应 `_migration/exec/PROMPT-INT2-R4.md`：
- K1 raw 订单 → M0 `data_import_run` 前向组装（`forward_m1_order_canonical`）；
- K2/K3 门里补的数据要能被装配看见（`request[tool]` + 顶层 `request` 双写）；
- K4 `solve_scheduling` 的 `routing_steps[].eligible_resources` 工位名兜底 + 分钟口径；
- K5 降级日历覆盖排产地平线（交期在过去）；
- K6 降级资源要覆盖**已存在**的 stations/persons/tooling（不只是新建对象）。
"""
import base64
import json
from datetime import date, datetime, timedelta, timezone

from yunpai_orchestrator.m0_import_store import CanonicalImportStore
from yunpai_orchestrator.orchestration_bridge import (
    _degrade_resource_snapshot,
    _degraded_calendar_snapshot,
    _standard_minutes_of,
    bridge_payload,
    forward_m1_order_canonical,
    read_supplier_facts,
)
from yunpai_orchestrator.pmc_v2_snapshots import finalize_snapshot, validate_resource_snapshot
from yunpai_orchestrator.reviewer.gates import apply_decision, make_gate

TZ = timezone(timedelta(hours=8))

ORDER_ATTACHMENT = {
    "kind": "order",
    "filename": "order-w913.xlsx",
    "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "content_b64": base64.b64encode(b"raw-xlsx-bytes").decode("ascii"),
}

M1_OUTPUT = {
    "task_id": "m1-task-1",
    "status": "needs_review",
    "document": {
        "schema_version": "m1.document.v2",
        "source": {"original_filename": "order-w913.xlsx", "sha256": "0" * 64},
        "header": {"order_id": "PO-20260625-003", "product_code": "W-H913",
                   "quantity": 15, "due_date": "2026-07-10"},
        "lines": [
            {"line_id": "PO-20260625-003::L1", "product_code": "W-H913", "quantity": 10, "uom": "PCS"},
            {"line_id": "PO-20260625-003::L2", "product_code": "W-H913", "quantity": 5, "uom": "PCS"},
        ],
    },
}


def _m1_state(extra_request=None):
    request = {"workflow": "m1_m5_document_to_plan",
               "documents": [dict(ORDER_ATTACHMENT)]}
    request.update(extra_request or {})
    return {
        "task_id": "task-w913", "tenant_id": "tenant-main", "site_id": "default",
        "request": request,
        "outputs": {"ingest_document": {"data": M1_OUTPUT}},
    }


# ---------------------------------------------------------------------------
# K1 · forward_m1_order_canonical
# ---------------------------------------------------------------------------

def test_k1_forward_m1_order_canonical_builds_structured_records(monkeypatch):
    monkeypatch.setenv("YUNPAI_M0_DB", "runtime/test-m0.sqlite")
    out = forward_m1_order_canonical([dict(ORDER_ATTACHMENT)], M1_OUTPUT)

    assert len(out) == 1
    assert out[0]["filename"] == "order-canonical.json"
    assert out[0]["content_type"] == "application/json"
    record = json.loads(base64.b64decode(out[0]["content_b64"]).decode("utf-8"))["records"][0]
    assert record["entity_type"] == "order"
    assert record["order_id"] == "PO-20260625-003"
    assert record["identity"] == {"business_key": "PO-20260625-003"}
    assert record["payload"]["product_code"] == "W-H913"
    assert record["payload"]["quantity"] == 15.0          # 行项数量求和，不编造
    assert record["payload"]["due_date"] == "2026-07-10"
    assert record["payload"]["lines"] == M1_OUTPUT["document"]["lines"]


def test_k1_forward_m1_order_canonical_is_noop_without_canonical_mode(monkeypatch):
    monkeypatch.delenv("YUNPAI_M0_DB", raising=False)
    files = [dict(ORDER_ATTACHMENT)]
    assert forward_m1_order_canonical(files, M1_OUTPUT) is files   # 沙箱模式行为不变


def test_k1_forward_m1_order_canonical_is_noop_when_inputs_insufficient(monkeypatch):
    monkeypatch.setenv("YUNPAI_M0_DB", "runtime/test-m0.sqlite")
    files = [dict(ORDER_ATTACHMENT)]
    # 已带 JSON 附件 → 不重复前向
    with_json = [{"filename": "order.json", "content_b64": "e30="}]
    assert forward_m1_order_canonical(with_json, M1_OUTPUT) is with_json
    # M1 没解析出行项 → 原样返回（由 canonical store 隔离，不伪造订单）
    no_lines = {"document": {"header": {"order_id": "PO-1"}, "lines": []}}
    assert forward_m1_order_canonical(files, no_lines) is files
    assert forward_m1_order_canonical(files, {}) is files


def test_k1_data_import_run_forwards_order_and_canonical_store_accepts(tmp_path, monkeypatch):
    """K1 端到端（桥接 + canonical store）：raw 附件不再被隔离成 no_structured_records。"""
    monkeypatch.setenv("YUNPAI_M0_DB", str(tmp_path / "m0.sqlite"))

    payload = bridge_payload(_m1_state(), "data_import_run")
    assert payload["files"][0]["filename"] == "order-canonical.json"

    store = CanonicalImportStore(tmp_path / "m0.sqlite")
    result = store.register_batch(task_id="task-w913", tenant_id="tenant-main",
                                  files=payload["files"])
    assert result["quarantined"] == []
    assert result["created_candidates"] == 1
    assert result["status"] == "awaiting_review"


def test_k1_data_import_run_keeps_raw_attachment_without_m1_lines(monkeypatch):
    """无 M1 行项时不前向：保持原「raw 附件直达」行为（失败关闭由 M0 侧负责）。"""
    monkeypatch.setenv("YUNPAI_M0_DB", "runtime/test-m0.sqlite")
    state = _m1_state()
    state["outputs"]["ingest_document"] = {"data": {"document": {"header": {"order_id": "PO-1"}, "lines": []}}}

    payload = bridge_payload(state, "data_import_run")
    assert payload["files"][0]["filename"] == "order-w913.xlsx"


# ---------------------------------------------------------------------------
# K2/K3 · 门里补的数据要能被装配看见
# ---------------------------------------------------------------------------

def _bom_outputs():
    return {
        "ingest_document": {"data": M1_OUTPUT},
        "run_bom_sop_workflow": {"data": {
            "bom_generation": {"approval_status": "approved", "bom_lines": [
                {"material_code": "YA.G.10.002", "quantity_per": 2, "uom": "PCS"},
            ]},
            "sop_generation": {"approval_status": "approved", "route_steps": [
                {"operation_id": "TX-001-01", "sequence": 1, "station": "脱皮",
                 "standard_minutes": 0.06},
            ]},
        }},
        "run_m3_procurement_requirements": {"data": {
            "procurement_plan_id": "plan-W-H913",
            "shortage_lines": [{"material_code": "YA.G.10.002", "material_name": "护套",
                                "shortage_qty": 4, "uom": "PCS"}],
        }},
    }


def test_k2_supplement_written_to_tool_payload_and_request_top_level():
    state = {"request": {"message": "m"}, "outputs": {}}
    gate = make_gate("blocked_input", "run_m3_procurement_requirements", "缺库存")
    updates = apply_decision(state, gate, {
        "decision": "retry", "actor": "u", "roles": ["data-steward"],
        "supplement": {"inventory_snapshot": [{"material_code": "YA.G.10.002", "available_qty": 9}]},
    })
    request = updates["request"]
    # 工具载荷兼容路径（assembler 显式参数）与装配读口（顶层）两条键路径都成立
    assert request["run_m3_procurement_requirements"]["inventory_snapshot"] == [
        {"material_code": "YA.G.10.002", "available_qty": 9}]
    assert request["inventory_snapshot"] == [{"material_code": "YA.G.10.002", "available_qty": 9}]


def test_k2_supplement_none_does_not_overwrite_top_level_fact():
    state = {"request": {"inventory_snapshot": [{"material_code": "KEEP", "available_qty": 1}]},
             "outputs": {}}
    gate = make_gate("blocked_input", "run_m3_procurement_requirements", "缺库存")
    updates = apply_decision(state, gate, {
        "decision": "retry", "actor": "u", "roles": ["data-steward"],
        "supplement": {"supplier_by_material": None, "degraded": True},
    })
    request = updates["request"]
    assert request["inventory_snapshot"] == [{"material_code": "KEEP", "available_qty": 1}]
    assert request["degraded"] is True
    assert "supplier_by_material" not in request


def test_k2_k3_bridge_reads_supplemented_inventory_and_supplier():
    state = {"task_id": "task-k2", "tenant_id": "tenant-main", "site_id": "default",
             "request": {"documents": [dict(ORDER_ATTACHMENT)]},
             "outputs": _bom_outputs()}

    m3_gate = make_gate("blocked_input", "run_m3_procurement_requirements", "缺库存")
    state.update(apply_decision(state, m3_gate, {
        "decision": "retry", "actor": "u", "roles": ["data-steward"],
        "supplement": {"inventory_snapshot": [{
            "material_code": "YA.G.10.002", "warehouse": "默认仓", "lot_no": "默认批次",
            "available_qty": 9, "locked_qty": 0, "qc_status": "released",
            "received_at": "2026-09-06T19:06:50+08:00"}]},
    }))

    m3_payload = bridge_payload(state, "run_m3_procurement_requirements")
    assert m3_payload.get("success") is not False, m3_payload
    assert [item["material_code"] for item in m3_payload["inventory_snapshot"]] == ["YA.G.10.002"]
    assert m3_payload["inventory_snapshot"][0]["available_qty"] == 9

    m4_gate = make_gate("procurement", "import_m4_purchase_suggestions_json", "缺供应商")
    state.update(apply_decision(state, m4_gate, {
        "decision": "retry", "actor": "u", "roles": ["procurement-manager"],
        "supplement": {"supplier_by_material": {"YA.G.10.002": "DEMO-演示供应商-非生产可用"}},
    }))

    assert read_supplier_facts(state) == {"YA.G.10.002": "DEMO-演示供应商-非生产可用"}
    m4_payload = bridge_payload(state, "import_m4_purchase_suggestions_json")
    assert m4_payload.get("success") is not False, m4_payload
    assert m4_payload["suggestions"][0]["supplier_name"] == "DEMO-演示供应商-非生产可用"


# ---------------------------------------------------------------------------
# K4 · 工序形状补齐（eligible_resources / 分钟口径）
# ---------------------------------------------------------------------------

def _solve_state(route_steps, *, degraded=True):
    request = {
        "documents": [dict(ORDER_ATTACHMENT)],
        "routing_steps": route_steps,
        "route_approval_ref": "APPROVED-W913",
        "route_code": "R-W-H913",
        "route_version": "v1",
    }
    if degraded:
        request["degraded"] = True
    outputs = _bom_outputs()
    # solve 要求上游 ingest_m5_planning_snapshot 已持久化回读（不重建事实）
    outputs["ingest_m5_planning_snapshot"] = {"data": {"readiness": {"status": "snapshots_stored"}}}
    return {"task_id": "task-k4", "tenant_id": "tenant-main", "site_id": "default",
            "request": request, "outputs": outputs}


def test_k4_eligible_resources_falls_back_to_station_name():
    """SOP 工序只带工位名时，eligible_resources 用（工位名，分钟）兜底。"""
    state = _solve_state([{"operation_id": "TX-001-04", "operation_name": "排卡",
                           "sequence": 1, "station": "排卡", "standard_minutes": 60}])
    payload = bridge_payload(state, "solve_scheduling")
    assert payload.get("success") is not False, payload
    step = payload["routing_steps"][0]
    assert step["eligible_resources"] == [{"resource_id": "排卡", "processing_minutes": 60}]


def test_k4_sub_minute_standard_minutes_clamped_to_one_minute():
    """合同 processing_minutes minimum=1：亚分钟工时不产出 0（否则合同校验失败）。"""
    state = _solve_state([{"operation_id": "TX-001-01", "operation_name": "脱外被",
                           "sequence": 1, "station": "脱皮", "standard_minutes": 0.06}])
    payload = bridge_payload(state, "solve_scheduling")
    assert payload["routing_steps"][0]["eligible_resources"][0]["processing_minutes"] == 1


def test_k4_standard_minutes_normalized_from_seconds():
    """工时口径 = 分钟：只有秒字段时显式 /60，字符串也归一。"""
    assert _standard_minutes_of({"standard_minutes": 12.9}) == 12.9
    assert _standard_minutes_of({"processing_minutes": "3.5"}) == 3.5
    assert _standard_minutes_of({"standard_time_s": 60}) == 1.0
    assert _standard_minutes_of({"standard_time": "90"}) == 1.5
    assert _standard_minutes_of({}) is None


# ---------------------------------------------------------------------------
# K5 · 降级日历覆盖排产地平线
# ---------------------------------------------------------------------------

def _calendar_days(snapshot):
    return sorted({item["start_at"][:10] for item in snapshot["working_intervals"]})


def test_k5_degraded_calendar_covers_past_due_date():
    state = _m1_state({"planning_start": "2026-07-10"})
    snapshot = _degraded_calendar_snapshot(state)
    days = _calendar_days(snapshot)
    today = datetime.now(TZ).date()
    due = date(2026, 7, 10)
    # 确定性规则：start = min(今天, 交期) - 1 天；end = max(今天, 交期) + 30 天
    first = min(today, due) - timedelta(days=1)
    last = max(today, due) + timedelta(days=30)

    assert "2026-07-10" in days, days          # 交期本身必须在窗口内
    assert days[0] == first.isoformat()
    assert days[-1] == last.isoformat()
    assert today.isoformat() in days           # 今天也在窗口内
    assert snapshot["degraded"] is True
    assert snapshot["source"] == "degraded-default-single-shift"
    for item in snapshot["working_intervals"]:
        assert item["calendar_ref"] == "CAL-DEGRADED"
        assert item["start_at"].endswith("T08:00:00+08:00")
        assert item["end_at"].endswith("T17:00:00+08:00")


def test_k5_degraded_calendar_falls_back_to_next_seven_days_without_horizon():
    snapshot = _degraded_calendar_snapshot({"task_id": "t", "request": {}, "outputs": {}})
    days = _calendar_days(snapshot)
    today = datetime.now(TZ).date()
    assert days == [(today + timedelta(days=i)).isoformat() for i in range(7)]


# ---------------------------------------------------------------------------
# K6 · 降级资源要覆盖「已存在的」工位/人员/模治具
# ---------------------------------------------------------------------------

def test_k6_degrade_resource_snapshot_fills_existing_station_fields():
    resource = {"snapshot_id": "SNAP-RES-M0-task", "revision": 1,
                "equipment": [], "persons": [], "tooling": [],
                "stations": [{"station_code": "排卡", "station_name": "排卡"}]}
    degraded = _degrade_resource_snapshot(resource, [])
    station = degraded["stations"][0]

    assert station["calendar_ref"] == "CAL-DEGRADED"
    assert station["status"] == "ACTIVE"
    assert station["parallel_slots"] == 1
    assert station["work_center_code"] == "排卡"
    assert station["degraded"] is True
    assert degraded["degraded"] is True


def test_k6_existing_station_snapshot_passes_strict_m5_validation():
    """K6 实证：canonical SOP 派生的工位缺 calendar_ref → 降级后过严格校验。"""
    resource = {"snapshot_id": "SNAP-RES-M0-task", "revision": 1,
                "equipment": [{"equipment_code": "EQ-1"}],
                "persons": [{"person_code": "P-1"}],
                "tooling": [{"tooling_code": "FIX-1"}],
                "stations": [{"station_code": "排卡", "station_name": "排卡"}]}
    validated = validate_resource_snapshot(finalize_snapshot(_degrade_resource_snapshot(resource, [])))

    assert validated["stations"][0]["calendar_ref"] == "CAL-DEGRADED"
    assert validated["persons"][0]["calendar_ref"] == "CAL-DEGRADED"
    assert validated["tooling"][0]["calendar_ref"] == "CAL-DEGRADED"
    assert validated["equipment"][0]["calendar_ref"] == "CAL-DEGRADED"


def test_k6_degrade_resource_snapshot_keeps_existing_facts():
    """兜底只填缺失字段，不覆盖已有权威事实。"""
    resource = {"snapshot_id": "SNAP-RES-M0-task", "revision": 2,
                "equipment": [], "persons": [], "tooling": [],
                "stations": [{"station_code": "焊接", "status": "MAINTENANCE",
                              "calendar_ref": "CAL-1", "parallel_slots": 3,
                              "work_center_code": "WC-9", "station_name": "焊接工位"}]}
    station = _degrade_resource_snapshot(resource, [])["stations"][0]
    assert station["status"] == "MAINTENANCE"
    assert station["calendar_ref"] == "CAL-1"
    assert station["parallel_slots"] == 3
    assert station["work_center_code"] == "WC-9"
