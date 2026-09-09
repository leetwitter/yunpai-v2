"""M4 本地工具端到端（rows-S5.md「需补测试（V2 零引用）」12 个 + P0 替换证据）。

覆盖工具（rows-S5.md:121 清单）：
``create_m4_supplier``、``generate_m4_purchase_inquiry_message``、``generate_m4_urge_message``、
``get_m4_material_supply_snapshot``、``get_m4_purchase_order``、``list_m4_material_supply_events``、
``list_m4_purchase_suggestions``、``list_m4_suppliers``、``request_changes_m4_purchase_order``、
``scan_m4_purchase_alerts``、``update_m4_supplier``（+ ``list_m4_purchase_orders``）。

同时锁定两条 P0：
- ``import_m4_purchase_suggestions_json`` 是**真实实现**（落库 + 同正文 replay + 异正文
  ``IDEMPOTENCY_CONFLICT``），旧 echo 版 ``workers.m4_purchase`` 已删除。
- ``send_m4_purchase_order`` 在受控 ctx（带 task_id）一律 ``LEGACY_SEND_DISABLED``。

全部经 ``registry.call`` 调用 → 输入/输出 JSON Schema 一并校验（不是直调 handler）。
"""
from __future__ import annotations

import pytest

from yunpai_orchestrator.m4_store import M4Store
from yunpai_orchestrator.m4b_store import M4BStore
from yunpai_orchestrator.registry import build_default_registry

TENANT = "TENANT-M4"
SITE = "SITE-1"
TASK = "TASK-M4-1"
CTX = {
    "task_id": TASK, "run_id": "RUN-M4-1", "tenant_id": TENANT, "trace_id": "RUN-M4-1",
    "idempotency_key": f"{TASK}:m4", "actor_user": "buyer-1", "actor_role": "buyer",
    "actor": "buyer-1",
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("YUNPAI_M4_DB", str(tmp_path / "m4.sqlite"))
    monkeypatch.setenv("YUNPAI_M4B_DB", str(tmp_path / "m4b.sqlite"))
    return tmp_path


@pytest.fixture
def registry(env):
    return build_default_registry()


def _suggestion(**overrides):
    item = {
        "item_code": "MAT-1", "item_name": "铜线", "quantity": 100, "unit": "M",
        "supplier_name": "SUP-1", "required_date": "2026-01-20", "project_code": "ORDER-1",
    }
    item.update(overrides)
    return item


async def _import(registry, *, suggestions=None, idem="IDEM-1"):
    return await registry.call(
        "import_m4_purchase_suggestions_json",
        {
            "suggestions": suggestions if suggestions is not None else [_suggestion()],
            "tenant_id": TENANT, "site_id": SITE,
            "tracking_task_id": TASK, "idempotency_key": idem,
        },
        CTX,
    )


async def _import_and_generate(registry, *, supplier_name="SUP-1", idem="IDEM-1"):
    imported = await _import(registry, suggestions=[_suggestion(supplier_name=supplier_name)],
                             idem=idem)
    orders = await registry.call(
        "generate_m4_purchase_orders",
        {"suggestion_item_ids": [imported["items"][0]["id"]]},
        CTX,
    )
    return imported, orders[0]


# ---------------------------------------------------------------------------
# P0：import_json 真实实现（替换 V2 旧 echo）
# ---------------------------------------------------------------------------

async def test_import_json_replaces_echo_with_persisted_idempotent_implementation(registry, env):
    from yunpai_orchestrator import workers

    assert not hasattr(workers, "m4_purchase"), "旧 echo 实现必须删除"

    imported = await _import(registry)
    assert imported["filename"] == "orchestrator_purchase_suggestions.csv"
    assert imported["total_rows"] == 1 and imported["valid_rows"] == 1
    assert imported["items"][0]["validation_status"] == "valid"
    assert imported["tracking_task_id"] == TASK
    # 真实落库（不是 sha256 回显）
    assert M4Store(str(env / "m4.sqlite")).get_batch(imported["id"]) is not None

    replay = await _import(registry)
    assert replay["id"] == imported["id"]  # 同 (task,key) 同正文 → replay

    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        await _import(registry, suggestions=[_suggestion(item_code="MAT-2")])
    with pytest.raises(ValueError, match="FORBIDDEN"):
        await registry.call(
            "import_m4_purchase_suggestions_json",
            {"suggestions": [_suggestion()], "tenant_id": "OTHER", "site_id": SITE,
             "tracking_task_id": TASK},
            CTX,
        )


# ---------------------------------------------------------------------------
# 建议 / 采购单读取（list_m4_purchase_suggestions、list_m4_purchase_orders、get_m4_purchase_order）
# ---------------------------------------------------------------------------

async def test_list_purchase_suggestions_orders_and_get_purchase_order(registry):
    imported, po = await _import_and_generate(registry)

    suggestions = await registry.call("list_m4_purchase_suggestions", {}, CTX)
    assert suggestions["total"] == 1
    assert suggestions["items"][0]["item_code"] == "MAT-1"

    orders = await registry.call("list_m4_purchase_orders", {}, CTX)
    assert orders["total"] == 1 and orders["items"][0]["status"] == "draft"

    fetched = await registry.call("get_m4_purchase_order", {"purchase_order_id": po["id"]}, CTX)
    assert fetched["purchase_order_no"] == po["purchase_order_no"]
    assert fetched["status"] == "draft"
    assert fetched["items"][0]["item_code"] == "MAT-1"
    # 实现额外返回 CAS 身份键（契约未设 additionalProperties:false → 放行）
    assert isinstance(fetched["current_revision"], int)
    assert isinstance(fetched["current_checksum"], str)
    assert imported["id"] == fetched["source_import_batch_id"]


async def test_get_m4_purchase_order_is_tenant_scoped(registry):
    _, po = await _import_and_generate(registry)
    with pytest.raises(ValueError, match="NOT_FOUND"):
        await registry.call("get_m4_purchase_order", {"purchase_order_id": po["id"]},
                            {**CTX, "tenant_id": "OTHER-TENANT"})


# ---------------------------------------------------------------------------
# 审核流转：request_changes_m4_purchase_order（+ submit/approve 供前置状态）
# ---------------------------------------------------------------------------

async def test_request_changes_m4_purchase_order(registry):
    _, po = await _import_and_generate(registry)
    submitted = await registry.call(
        "submit_m4_purchase_order_review",
        {"purchase_order_id": po["id"], "expected_revision": po["current_revision"],
         "expected_checksum": po["current_checksum"]},
        CTX,
    )
    assert submitted["status"] == "pending_review"

    changed = await registry.call(
        "request_changes_m4_purchase_order",
        {"purchase_order_id": po["id"], "expected_revision": submitted["current_revision"],
         "expected_checksum": submitted["current_checksum"], "comment": "交期需重报"},
        CTX,
    )
    assert changed["status"] == "request_changes"


# ---------------------------------------------------------------------------
# 询价草稿：generate_m4_purchase_inquiry_message
# ---------------------------------------------------------------------------

async def test_generate_m4_purchase_inquiry_message_is_deterministic_draft(registry):
    _, po = await _import_and_generate(registry)
    submitted = await registry.call(
        "submit_m4_purchase_order_review",
        {"purchase_order_id": po["id"], "expected_revision": po["current_revision"],
         "expected_checksum": po["current_checksum"]},
        CTX,
    )
    approved = await registry.call(
        "approve_m4_purchase_order",
        {"purchase_order_id": po["id"], "expected_revision": submitted["current_revision"],
         "expected_checksum": submitted["current_checksum"]},
        CTX,
    )
    assert approved["status"] == "approved_for_message"

    payload = {
        "purchase_order_id": po["id"],
        "expected_po_revision": approved["current_revision"],
        "expected_po_checksum": approved["current_checksum"],
        "recipient_snapshot": {"email": "supplier@example.com"},
        "channel": "email",
        "language": "zh-CN",
    }
    message = await registry.call("generate_m4_purchase_inquiry_message", payload, CTX)
    assert message["channel"] == "email"
    assert message["recipient"] == "supplier@example.com"
    assert message["send_status"] == "generated"
    assert message["model_name"] is None and message["prompt_version"] is None  # 确定性模板
    assert "MAT-1" in message["content"] and "100 M" in message["content"]
    assert message["subject"] == f"采购询价 {po['purchase_order_no']}"

    replay = await registry.call("generate_m4_purchase_inquiry_message", payload, CTX)
    assert replay["message_id"] == message["message_id"]  # 同 PO revision 幂等

    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        await registry.call("generate_m4_purchase_inquiry_message",
                            {**payload, "channel": "sms"}, CTX)


async def test_send_m4_purchase_order_refuses_controlled_context(registry):
    _, po = await _import_and_generate(registry)
    with pytest.raises(ValueError, match="LEGACY_SEND_DISABLED"):
        await registry.call("send_m4_purchase_order", {"purchase_order_id": po["id"]}, CTX)


# ---------------------------------------------------------------------------
# 供应商主数据：create_m4_supplier / update_m4_supplier / list_m4_suppliers
# ---------------------------------------------------------------------------

async def test_create_update_and_list_m4_supplier(registry, env):
    created = await registry.call(
        "create_m4_supplier",
        {"supplier_name": "宁波精工", "supplier_code": "SUP-CODE-1",
         "contact_name": "李工", "email": "li@example.com", "phone": "13800000000",
         "default_channel": "wechat", "remark": "首选"},
        CTX,
    )
    assert created["supplier_name"] == "宁波精工"
    assert created["default_channel"] == "wechat" and created["status"] == "active"
    # R5 新增入参 supplier_code 必须真的落库（不是被 schema 之外静默丢弃）
    row = M4BStore(str(env / "m4b.sqlite")).supplier_by_code(TENANT, "SUP-CODE-1")
    assert row is not None and row["supplier_code"] == "SUP-CODE-1"

    listed = await registry.call("list_m4_suppliers", {"supplier_name": "精工"}, CTX)
    assert listed["total"] == 1 and listed["items"][0]["id"] == created["id"]

    updated = await registry.call(
        "update_m4_supplier",
        {"supplier_id": created["id"], "phone": "13900000000", "status": "inactive"},
        CTX,
    )
    assert updated["phone"] == "13900000000" and updated["status"] == "inactive"
    assert updated["supplier_name"] == "宁波精工"  # PATCH 语义：未提供键不动

    with pytest.raises(ValueError, match="DUPLICATE_SUPPLIER"):
        await registry.call("create_m4_supplier", {"supplier_name": "宁波精工"}, CTX)
    with pytest.raises(ValueError, match="VALIDATION_ERROR"):
        await registry.call("update_m4_supplier",
                            {"supplier_id": created["id"], "email": "not-an-email"}, CTX)


# ---------------------------------------------------------------------------
# 供应商回复：create → parse → confirm → fact（含追踪回写）
# ---------------------------------------------------------------------------

async def _create_reply(registry, *, po_id=1, po_no="PO-EXT-1", content=None):
    return await registry.call(
        "create_m4_supplier_reply",
        {"purchase_order_id": po_id, "purchase_order_no": po_no, "supplier_name": "SUP-1",
         "reply_content": content or "可于 2026-02-01 交货，单价 12.5 元，含税"},
        CTX,
    )


async def test_supplier_reply_parse_confirm_and_fact(registry, env):
    reply = await _create_reply(registry)
    assert reply["purchase_order_no"] == "PO-EXT-1"

    parsed = await registry.call("parse_m4_supplier_reply", {"reply_id": reply["id"]}, CTX)
    assert parsed["need_human_review"] is True          # B 型降级显式标注
    assert parsed["confidence"] < 0.5
    assert parsed["delivery_date"] == "2026-02-01"
    assert parsed["unit_price"] == "12.5" and parsed["currency"] == "CNY"
    assert parsed["tax_included"] is True

    confirmed = await registry.call(
        "confirm_m4_supplier_reply",
        {"reply_id": reply["id"], "need_human_review": False, "delivery_date": "2026-02-01",
         "unit_price": "12.5", "currency": "CNY", "tax_included": True, "confidence": 0.98},
        CTX,
    )
    assert confirmed["need_human_review"] is False
    assert confirmed["delivery_date"] == "2026-02-01"

    store = M4BStore(str(env / "m4b.sqlite"))
    version = store.reply_get(TENANT, reply["id"])["parsed_result_version"]
    fact = await registry.call(
        "confirm_m4_supplier_fact",
        {"reply_id": reply["id"],
         "schema_version": "m4.supplier-fact-confirmation-request.v1",
         "purchase_order_item_id": 21, "supplier_reply_version": version,
         "result": {"delivery_date": "2026-02-01"}},
        CTX,
    )
    assert fact["confirmed_by"] == "buyer-1"            # ctx.actor 映射生效
    assert fact["checksum"].startswith("sha256:")
    assert fact["purchase_order_item_id"] == 21

    replay = await registry.call(
        "confirm_m4_supplier_fact",
        {"reply_id": reply["id"],
         "schema_version": "m4.supplier-fact-confirmation-request.v1",
         "purchase_order_item_id": 21, "supplier_reply_version": version,
         "result": {"delivery_date": "2026-02-01"}},
        CTX,
    )
    assert replay["id"] == fact["id"]                   # 幂等 replay


# ---------------------------------------------------------------------------
# 追踪 / 预警 / 催单
# ---------------------------------------------------------------------------

async def test_list_m4_tracking_after_confirmed_reply(registry):
    reply = await _create_reply(registry, po_id=7, po_no="PO-EXT-7")
    await registry.call(
        "confirm_m4_supplier_reply",
        {"reply_id": reply["id"], "need_human_review": False, "delivery_date": "2026-02-01",
         "unit_price": "12.5", "currency": "CNY", "confidence": 0.98},
        CTX,
    )
    tracking = await registry.call("list_m4_tracking", {}, CTX)
    assert tracking["total"] == 1
    item = tracking["items"][0]
    assert item["purchase_order_no"] == "PO-EXT-7"
    assert item["promised_date"] == "2026-02-01"
    assert item["arrival_status"] == "not_received"


async def test_scan_list_alerts_and_generate_urge_message(registry):
    reply = await _create_reply(registry, po_id=9, po_no="PO-EXT-9")
    await registry.call(
        "confirm_m4_supplier_reply",
        {"reply_id": reply["id"], "need_human_review": False, "delivery_date": "2020-01-01",
         "unit_price": "12.5", "currency": "CNY", "confidence": 0.98},
        CTX,
    )
    scan = await registry.call("scan_m4_purchase_alerts", {}, CTX)
    assert scan["scanned"] == 1 and scan["created"] >= 1

    alerts = await registry.call("list_m4_purchase_alerts", {"alert_type": "overdue"}, CTX)
    assert alerts["total"] == 1
    alert = alerts["items"][0]
    assert alert["alert_type"] == "overdue" and alert["days_overdue"] > 0

    urge = await registry.call("generate_m4_urge_message",
                               {"alert_id": alert["id"], "channel": "wechat"}, CTX)
    assert urge["alert_id"] == alert["id"]
    assert "PO-EXT-9" in urge["urge_message"]
    assert urge["model_name"] is None
    assert urge["prompt_version"] == "m4-urge-rule.v1"

    rescanned = await registry.call("scan_m4_purchase_alerts", {}, CTX)
    assert rescanned["created"] == 0  # open 去重：不重复造预警


# ---------------------------------------------------------------------------
# 供应快照 / 事件流
# ---------------------------------------------------------------------------

_QUERY = {
    "schema_version": "m5.material-supply-query.v1",
    "scenario_id": "SCN-1", "tenant_id": TENANT, "site_id": SITE,
    "material_ids": ["MAT-1"], "as_of": "2026-01-01T00:00:00Z",
    "horizon_end": "2026-02-01T00:00:00Z",
}


async def test_query_and_get_material_supply_snapshot(registry):
    snapshot = await registry.call("query_m4_material_supply_snapshot", dict(_QUERY), CTX)
    body = snapshot["data"]  # registry 包装后的信封，业务形状在 data
    assert body["schema_version"] == "m5.material-supply-snapshot.v1"
    assert body["snapshot_version"] == 1
    assert body["lines"] == []
    assert body["completeness"] == {
        "is_complete": False, "missing_facts": ["purchase_order_line_registry"]}
    assert body["checksum"].startswith("sha256:")

    replay = await registry.call("query_m4_material_supply_snapshot", dict(_QUERY), CTX)
    assert replay["data"]["snapshot_id"] == body["snapshot_id"]
    assert replay["data"]["snapshot_version"] == 1  # 幂等：同输入不追加版本

    fetched = await registry.call(
        "get_m4_material_supply_snapshot",
        {"snapshot_id": body["snapshot_id"], "snapshot_version": 1},
        CTX,
    )
    assert fetched["data"] == body  # 精确 immutable 回读

    with pytest.raises(ValueError, match="NOT_FOUND"):
        await registry.call("get_m4_material_supply_snapshot",
                            {"snapshot_id": body["snapshot_id"], "snapshot_version": 99}, CTX)


async def test_list_m4_material_supply_events_empty_stream(registry):
    page = await registry.call("list_m4_material_supply_events", {"site_id": SITE}, CTX)
    assert page["data"] == {
        "schema_version": "m5.material-supply-event-page.v1",
        "site_id": SITE, "after_version": 0, "items": [], "has_more": False,
        "next_after_version": 0,
    }
    # 合同必填校验在 registry 层先拦住（handler 不会被调用）
    with pytest.raises(ValueError, match="required property"):
        await registry.call("list_m4_material_supply_events", {}, CTX)
