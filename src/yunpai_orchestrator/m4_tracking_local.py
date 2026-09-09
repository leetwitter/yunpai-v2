"""M4 追踪：追踪聚合/预警扫描与催单/供应快照与事件流。

迁移来源（只读）：``_wt/INT/src/yunpai_langgraph/m4_tracking_local.py``
（INT 源 sha256 ``8006fdcf3146908a8766a75935081e9ea83b62ac01abb3c0a91254ce1d279965``）

- 数据落 ``m4b_store.M4BStore``（env ``YUNPAI_M4B_DB``）；tenant 严格隔离：身份只信
  ctx（``tenant_id``）；站点从 payload 读取并只读本租户流。
- 快照语义：查询时惰性聚合落快照表（幂等 replay / version 单调追加 / 精确读取 immutable）。
  投影数据源=PO 行（M4A 归属）——本线无权威 PO 行注册表 → 诚实输出 ``lines=[]`` 并以
  ``completeness.missing_facts=['purchase_order_line_registry']`` 显式标注；只读工具不伪造。
- 催单话术 = 模型 C 型以 B 降级：确定性模板 + ``model_name=null``、
  ``prompt_version=m4-urge-rule.v1`` 显式标注（R046 降级路径断言）。
- 错误风格：``ValueError("CODE: 消息")``；输出形状以 manifest 各工具 output_schema 为准
  （query/get snapshot、list events 为 ``additionalProperties:false`` 严格键集）。

V2 迁移差异（rows-S5.md）：
- ``receive_m4_schedule_impact_proposal`` 判「不搬（设计如此）」——``binding.py`` 的
  ``ORCHESTRATION_INTERNAL`` 保持其 UNBOUND，本模块**不提供**该 handler（能力缺口已登记
  ``REQUESTS-MIG-M4.md``，不得登记进 ``HANDLERS``）。
- P0-1：``query_m4_material_supply_snapshot`` 查询即**惰性写快照** → 已从
  ``M4_READ_ONLY_SKILL_OPERATIONS`` 摘除，并在 ``reviewer/rules.py`` 挂 ``procurement`` 门。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .m4b_store import (
    M4BStore,
    _as_utc_timestamp,
    payload_checksum,
)

URGE_RULE_PROMPT_VERSION = "m4-urge-rule.v1"

_SCHEMA_QUERY = "m5.material-supply-query.v1"
_SCHEMA_SNAPSHOT = "m5.material-supply-snapshot.v1"
_SCHEMA_EVENT_PAGE = "m5.material-supply-event-page.v1"

_MISSING_LINE_REGISTRY = "purchase_order_line_registry"
_RECEIVED_OR_CLOSED = {"received", "closed_exception"}


def _store() -> M4BStore:
    return M4BStore()


def _tenant(ctx: dict[str, Any]) -> str:
    return str(ctx.get("tenant_id") or "default")


def _task(ctx: dict[str, Any]) -> str:
    return str(ctx.get("task_id") or "local")


def _page_args(payload: dict[str, Any]) -> tuple[int, int]:
    page = max(int(payload.get("page") or 1), 1)
    page_size = max(min(int(payload.get("page_size") or 20), 200), 1)
    return page, page_size


def _json_value(text: str, fallback: Any) -> Any:
    import json

    if not text:
        return fallback
    try:
        return json.loads(text)
    except ValueError:
        return fallback


# ---------------------------------------------------------------------------
# list_m4_tracking
# ---------------------------------------------------------------------------

def _tracking_item(row: dict[str, Any]) -> dict[str, Any]:
    item_ref = row["purchase_order_item_id"]
    return {
        "id": int(row["id"]),
        "purchase_order_id": row["purchase_order_id"],
        "purchase_order_no": row["purchase_order_no"],
        "purchase_order_item_id": (int(item_ref) if item_ref.isdigit() else item_ref) if item_ref else None,
        "supplier_name": row["supplier_name"] or None,
        "promised_date": row["promised_date"],
        "unit_price": row["unit_price"],
        "currency": row["currency"],
        "exception_type": row["exception_type"],
        "exception_description": row["exception_description"],
        "arrival_status": row["arrival_status"],
        "is_overdue": bool(row["is_overdue"]),
    }


async def list_m4_tracking(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """采购追踪列表（承诺交期/价格/异常/超期；写后读自证源）。"""
    page, page_size = _page_args(payload)
    rows, total = _store().tracking_list(
        _tenant(ctx), offset=(page - 1) * page_size, limit=page_size)
    return {"items": [_tracking_item(r) for r in rows], "page": page,
            "page_size": page_size, "total": total}


# ---------------------------------------------------------------------------
# alerts：scan / list / urge
# ---------------------------------------------------------------------------

def _classify_alert(today: date, promised_date: date, due_soon_days: int = 3) -> str | None:
    days = (promised_date - today).days
    if days < 0:
        return "overdue"
    if days <= due_soon_days:
        return "due_soon"
    return None


async def scan_m4_purchase_alerts(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """预警扫描：对追踪承诺生成 overdue/due_soon/supplier_exception 预警（open 去重）。"""
    today = date.today()
    store = _store()
    tenant = _tenant(ctx)
    rows, _total = store.tracking_list(tenant, offset=0, limit=10_000_000)
    scanned = 0
    created = 0
    for row in rows:
        if row["arrival_status"] in _RECEIVED_OR_CLOSED or not row["promised_date"]:
            continue
        try:
            promised = date.fromisoformat(row["promised_date"])
        except ValueError:
            continue
        scanned += 1
        alert_type = _classify_alert(today, promised)
        if alert_type:
            days_overdue = max((today - promised).days, 0)
            existing = store.alert_find_open(
                tenant, row["purchase_order_id"], row["purchase_order_item_id"], alert_type)
            if existing is None:
                store.alert_create(
                    tenant_id=tenant, alert_type=alert_type,
                    purchase_order_id=row["purchase_order_id"],
                    purchase_order_item_id=row["purchase_order_item_id"],
                    purchase_order_no=row["purchase_order_no"],
                    supplier_name=row["supplier_name"],
                    item_code=None, item_name=None,
                    promised_date=row["promised_date"], days_overdue=days_overdue,
                    urge_message=None)
                created += 1
            if alert_type == "overdue" and not bool(row["is_overdue"]):
                store.tracking_mark_overdue(tenant, int(row["id"]), True)
        if row["exception_description"]:
            existing = store.alert_find_open(
                tenant, row["purchase_order_id"], row["purchase_order_item_id"],
                "supplier_exception")
            if existing is None:
                store.alert_create(
                    tenant_id=tenant, alert_type="supplier_exception",
                    purchase_order_id=row["purchase_order_id"],
                    purchase_order_item_id=row["purchase_order_item_id"],
                    purchase_order_no=row["purchase_order_no"],
                    supplier_name=row["supplier_name"],
                    item_code=None, item_name=None,
                    promised_date=row["promised_date"],
                    days_overdue=max((today - promised).days, 0),
                    urge_message=None,
                    exception_description=row["exception_description"][:512])
                created += 1
    return {"scanned": scanned, "created": created}


def _alert_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "alert_type": row["alert_type"],
        "purchase_order_no": row["purchase_order_no"],
        "supplier_name": row["supplier_name"],
        "item_code": row["item_code"],
        "item_name": row["item_name"],
        "promised_date": row["promised_date"],
        "days_overdue": int(row["days_overdue"] or 0),
        "urge_message": row["urge_message"],
        "status": row["status"],
    }


async def list_m4_purchase_alerts(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """预警列表（status / alert_type 过滤 + 分页）。"""
    page, page_size = _page_args(payload)
    status = str(payload.get("status") or "") or None
    alert_type = str(payload.get("alert_type") or "") or None
    rows, total = _store().alert_list(
        _tenant(ctx), status=status, alert_type=alert_type,
        offset=(page - 1) * page_size, limit=page_size)
    return {"items": [_alert_item(r) for r in rows], "page": page,
            "page_size": page_size, "total": total}


_CHANNEL_LABEL = {
    "email": ("邮件", "正式、完整、结构清晰，包含称呼、催单背景、交期风险和结束语。"),
    "wechat": ("微信", "口语化但保持商务礼貌，短句为主，适合直接粘贴到微信聊天。"),
    "dingtalk": ("钉钉", "简洁、工作流风格，重点突出待确认事项。"),
    "feishu": ("飞书", "清晰、协作化，避免过长寒暄。"),
    "enterprise_wechat": ("企业微信", "商务礼貌、简洁。"),
    "sms": ("短信", "极简短，保留采购单号、物料摘要、承诺交期和催单要求。"),
    "manual": ("手工发送", "通用采购催单语气，便于复制后自行调整。"),
}


def _urge_template(alert_type: str, days_overdue: int, *, english: bool) -> str:
    """确定性话术模板（按 alert_type；C 型降级显式标注走 model_name/prompt_version）。"""
    if english:
        if alert_type == "overdue":
            return ("The promised delivery date of your order has been exceeded by {days} "
                    "day(s). Please confirm the latest delivery date as soon as possible.")
        if alert_type == "due_soon":
            return "Your order is due soon. Please confirm that delivery will be on time."
        return ("There is an exception in your latest reply. Please clarify the delivery "
                "commitment as soon as possible.")
    if alert_type == "overdue":
        return ("采购单 {po_no}（{item}，承诺交期 {promised}）已逾期 {days} 天，"
                "请尽快确认最新交期并回复。")
    if alert_type == "due_soon":
        return ("采购单 {po_no}（{item}，承诺交期 {promised}）即将到期，"
                "请确认可按时交付。")
    return ("贵司对采购单 {po_no} 的最新回复存在异常（{exception}），"
            "请尽快澄清实际交期或供应情况。")


def _urge_message_text(alert: dict[str, Any], channel: str, language: str | None) -> str:
    english = bool(language and language.lower().startswith("en"))
    label, _tone = _CHANNEL_LABEL.get(channel, _CHANNEL_LABEL["wechat"])
    del label
    promised = alert["promised_date"] or "未确认交期"
    item_summary = "物料" + ((" " + alert["item_code"]) if alert["item_code"] else "") \
        + ((" " + alert["item_name"]) if alert["item_name"] else "")
    body = _urge_template(alert["alert_type"], int(alert["days_overdue"] or 0),
                          english=english).format(
        po_no=alert["purchase_order_no"], item=item_summary.strip() or "物料行",
        promised=promised, days=int(alert["days_overdue"] or 0),
        exception=(alert.get("exception_description") or "异常说明")[:80])
    greeting = f"供应商 {alert['supplier_name']}，" if alert.get("supplier_name") else ""
    if channel == "sms":
        text = f"{greeting}{body}" if greeting else body
    elif english:
        text = f"Dear supplier,\n{body}\nBest regards."
    else:
        text = f"{greeting}\n{body}" if greeting else body
    return text.strip()


async def generate_m4_urge_message(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """为指定预警生成催单草稿（仅 open；覆盖既有催单文本；不发送）。

    C/B 降级：确定性模板（model_name=null、prompt_version=m4-urge-rule.v1）。
    """
    alert_id = int(payload.get("alert_id"))
    alert = _store().alert_get(_tenant(ctx), alert_id)
    if alert is None:
        raise ValueError(f"NOT_FOUND: 采购预警不存在: {alert_id}")
    if alert["status"] != "open":
        raise ValueError(f"INVALID_STATUS: 只有打开状态的预警可以生成催单文本: {alert_id}")
    channel = str(payload.get("channel") or "wechat")
    if channel not in _CHANNEL_LABEL:
        raise ValueError("VALIDATION_ERROR: channel 不在支持集合内")
    language = payload.get("language")
    language = str(language) if language else None
    text = _urge_message_text(alert, channel, language)
    updated = _store().alert_set_urge(_tenant(ctx), alert_id, text)
    if updated is None:
        raise ValueError(f"NOT_FOUND: 采购预警不存在: {alert_id}")
    return {
        "alert_id": int(updated["id"]),
        "urge_message": updated["urge_message"] or "",
        "channel": channel,
        "model_name": None,
        "prompt_version": URGE_RULE_PROMPT_VERSION,
    }


# ---------------------------------------------------------------------------
# 供应快照：query（惰性持久化）/ get（精确 immutable）
# ---------------------------------------------------------------------------

def _parse_datetime(value: Any, field: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"VALIDATION_ERROR: {field} 必填")
    try:
        return _as_utc_timestamp(text)
    except ValueError as exc:
        raise ValueError(f"VALIDATION_ERROR: {field} 必须为 RFC 3339: {text!r}") from exc


def _normalize_query(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") != _SCHEMA_QUERY:
        raise ValueError(
            f"VALIDATION_ERROR: schema_version 必须为 {_SCHEMA_QUERY}")
    required = ("scenario_id", "tenant_id", "site_id", "material_ids", "as_of", "horizon_end")
    for key in required:
        if payload.get(key) in (None, ""):
            raise ValueError(f"VALIDATION_ERROR: {key} 必填")
    material_ids = payload["material_ids"]
    if not isinstance(material_ids, list) or not material_ids:
        raise ValueError("VALIDATION_ERROR: material_ids 至少一项")
    normalized = {
        "schema_version": _SCHEMA_QUERY,
        "scenario_id": str(payload["scenario_id"]),
        "tenant_id": str(payload["tenant_id"]),
        "site_id": str(payload["site_id"]),
        "material_ids": sorted({str(item) for item in material_ids}),
        "as_of": payload["as_of"],
        "horizon_end": payload["horizon_end"],
    }
    for key in ("as_of", "horizon_end"):
        _parse_datetime(normalized[key], key)
    if _parse_datetime(normalized["horizon_end"], "horizon_end") < \
            _parse_datetime(normalized["as_of"], "as_of"):
        raise ValueError("VALIDATION_ERROR: horizon_end 必须 >= as_of")
    return normalized


def _current_snapshot_id(tenant_id: str, input_checksum: str, observed_at: str) -> str:
    identity = {
        "tenant_id": tenant_id,
        "input_checksum": input_checksum,
        "observed_at": observed_at,
        "line_versions": [],
    }
    return "current-" + payload_checksum(identity).removeprefix("sha256:")


def _empty_projection(query: dict[str, Any]) -> dict[str, Any]:
    """本线投影：无 M4A PO 行注册表 → 无行 + 显式 missing_fact（不伪造，差异 7-06）。"""
    as_of_text = str(query["as_of"])
    return {
        "lines": [],
        "completeness": {
            "is_complete": False,
            "missing_facts": [_MISSING_LINE_REGISTRY],
        },
        "observed_at": as_of_text,
    }


async def query_m4_material_supply_snapshot(payload: dict[str, Any],
                                            ctx: dict[str, Any]) -> dict[str, Any]:
    """按 scope/物料/窗口读取当前供应投影；查询即惰性持久化（幂等/version 单调）。

    ⚠️ 本工具虽然名字是「查询」，但会把投影**持久化**进快照表（惰性写入）→ 不是只读工具：
    已从 ``M4_READ_ONLY_SKILL_OPERATIONS`` 摘除，并在 ``reviewer/rules.py`` 挂
    ``procurement`` 人工门（P0-1）。
    """
    query = _normalize_query(payload)
    tenant = _tenant(ctx)
    if query["tenant_id"] != tenant:
        raise ValueError(
            "FORBIDDEN: material supply scope does not allow this resource: tenant_scope_mismatch")
    input_checksum = payload_checksum(query)
    store = _store()
    tracking_task_id = _task(ctx)
    idempotency_key = f"{tracking_task_id}:{input_checksum}"
    existing = store.snapshot_find_by_task_key(tracking_task_id, idempotency_key)
    if existing is not None:
        if existing["tenant_id"] != tenant:
            raise ValueError(
                "FORBIDDEN: material supply scope does not allow this resource: "
                "tenant_scope_mismatch")
        if existing["input_checksum"] != input_checksum:
            raise ValueError(
                "IDEMPOTENCY_CONFLICT: material supply idempotency conflict: "
                "canonical_input_mismatch")
        return _response_of(existing, tenant)

    projection = _empty_projection(query)
    snapshot_id = _current_snapshot_id(tenant, input_checksum, projection["observed_at"])
    snapshot_version = store.snapshot_next_version(snapshot_id)
    snapshot = {
        "schema_version": _SCHEMA_SNAPSHOT,
        "snapshot_id": snapshot_id,
        "snapshot_version": snapshot_version,
        "scenario_id": query["scenario_id"],
        "tenant_id": tenant,
        "site_id": query["site_id"],
        "as_of": query["as_of"],
        "horizon_end": query["horizon_end"],
        "generated_at": projection["observed_at"],
        "observed_at": projection["observed_at"],
        "entity_version": "m4-current-projection-v1",
        "lines": projection["lines"],
        "completeness": projection["completeness"],
        "input": query,
        "input_checksum": input_checksum,
    }
    snapshot["checksum"] = payload_checksum(snapshot, exclude="checksum")
    store.snapshot_insert(
        snapshot_id=snapshot_id, snapshot_version=snapshot_version,
        tenant_id=tenant, site_id=query["site_id"],
        tracking_task_id=tracking_task_id, idempotency_key=idempotency_key,
        input_checksum=input_checksum, checksum=snapshot["checksum"],
        generated_at=snapshot["generated_at"], observed_at=snapshot["observed_at"],
        response=snapshot)
    return snapshot


def _response_of(row: dict[str, Any], tenant: str) -> dict[str, Any]:
    if row["tenant_id"] != tenant:
        raise ValueError(
            "FORBIDDEN: material supply scope does not allow this resource: tenant_scope_mismatch")
    response = _json_value(row["response_json"], None)
    if not isinstance(response, dict):
        raise ValueError("SOURCE_UNAVAILABLE: persisted snapshot payload is corrupted")
    checksum = payload_checksum(response, exclude="checksum")
    if checksum != row["checksum"] or checksum != response.get("checksum"):
        raise ValueError("SOURCE_UNAVAILABLE: persisted snapshot checksum mismatch")
    return response


async def get_m4_material_supply_snapshot(payload: dict[str, Any],
                                          ctx: dict[str, Any]) -> dict[str, Any]:
    """按 snapshot_id + snapshot_version 精确读取 immutable 快照（绝不以最新替代请求版本）。"""
    snapshot_id = str(payload.get("snapshot_id") or "")
    try:
        snapshot_version = int(payload.get("snapshot_version"))
    except (TypeError, ValueError) as exc:
        raise ValueError("VALIDATION_ERROR: snapshot_version 必须为整数") from exc
    if not snapshot_id or snapshot_version < 1:
        raise ValueError("VALIDATION_ERROR: snapshot_id / snapshot_version(>=1) 必填")
    row = _store().snapshot_get_exact(snapshot_id, snapshot_version)
    if row is None:
        raise ValueError("NOT_FOUND: material supply resource not found: snapshot_not_found")
    return _response_of(row, _tenant(ctx))


# ---------------------------------------------------------------------------
# 供应事件流：list（版本游标；同一 (tenant,site) 流内断点续取）
# ---------------------------------------------------------------------------

async def list_m4_material_supply_events(payload: dict[str, Any],
                                         ctx: dict[str, Any]) -> dict[str, Any]:
    """增量读取本租户/站点事件流（after_version 游标；严格 (tenant_id, site_id) 隔离）。"""
    site_id = str(payload.get("site_id") or "")
    if not site_id:
        raise ValueError("VALIDATION_ERROR: site_id 必填")
    try:
        after_version = int(payload.get("after_version") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("VALIDATION_ERROR: after_version 必须为整数") from exc
    after_version = max(after_version, 0)
    try:
        limit = min(int(payload.get("limit") or 100), 500)
    except (TypeError, ValueError) as exc:
        raise ValueError("VALIDATION_ERROR: limit 必须为整数") from exc
    items, has_more = _store().event_list(
        _tenant(ctx), site_id, after_version=after_version, limit=max(limit, 1))
    next_after_version = items[-1]["event_version"] if items else after_version
    return {
        "schema_version": _SCHEMA_EVENT_PAGE,
        "site_id": site_id,
        "after_version": after_version,
        "items": items,
        "has_more": has_more,
        "next_after_version": next_after_version,
    }


#: 本地 M4 追踪 handler 键集。``receive_m4_schedule_impact_proposal`` 按
#: rows-S5.md:76 判「不搬（设计如此）」（ORCHESTRATION_INTERNAL），故不在本表。
LOCAL_HANDLERS: dict[str, Any] = {
    "list_m4_tracking": list_m4_tracking,
    "scan_m4_purchase_alerts": scan_m4_purchase_alerts,
    "list_m4_purchase_alerts": list_m4_purchase_alerts,
    "generate_m4_urge_message": generate_m4_urge_message,
    "query_m4_material_supply_snapshot": query_m4_material_supply_snapshot,
    "list_m4_material_supply_events": list_m4_material_supply_events,
    "get_m4_material_supply_snapshot": get_m4_material_supply_snapshot,
}
