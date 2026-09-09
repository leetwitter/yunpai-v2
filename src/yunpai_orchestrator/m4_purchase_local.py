"""M4 采购本地 handler：建议导入（JSON）、PO 生成与审核流转、询价消息、发送守卫。

迁移来源（只读）：``_wt/INT/src/yunpai_langgraph/m4_purchase_local.py``
（INT 源 sha256 ``116f20cca105bb2a953ec038769541258b153fabbc2881686b6f14c5dddffe77``）

- 每工具 ``async handler(payload, ctx)`` + 模块级 ``LOCAL_HANDLERS`` 导出，
  由 ``workers._m4()`` 惰性装配进 ``HANDLERS``。
- 契约输出直返形状（数组工具返数组），错误 raise ``ValueError("CODE: 消息")``；
  TaskID/actor 只信 ctx；tenant 权威 = ``ctx.tenant_id``（payload 显式不一致 → FORBIDDEN）。
- 存储：``M4Store``（sqlite；``ctx.m4_db_path`` → ``YUNPAI_M4_DB`` → ``runtime/yunpai-m4.sqlite``）。
- 装配键：V2 走 ``request[tool]``（``worker/assembler.py:58``），不再是 ``request.payloads[tool]``。

V2 迁移差异（rows-S5.md）：
- ⑧ ``import_m4_purchase_suggestions_json`` 替换 V2 旧 echo 实现（旧 ``workers.m4_purchase``
  已删除）；该工具随之移出 ``binding.SANDBOX_TOOLS``（P0）。
- ③ ``_actor`` 兼容 V2 ctx（``actor`` 为 ``actor_user`` 的同源别名，``INFRA-DECISIONS §1.2``）。
- ⑤ 幂等键由 ``orchestration_bridge`` 按交接载荷内容派生（R049 新版桥）。
- CSV 变体 ``import_m4_purchase_suggestions`` 按 rows-S5.md:66 **废弃不搬**（契约保留，
  无生产调用方；本模块不再提供该 handler，避免误登记进 ``HANDLERS``）。
"""

from __future__ import annotations

import csv
import json
import os
from io import StringIO
from typing import Any, Awaitable, Callable

from .m4_store import (
    CSV_EXPECTED_FIELDS,
    CSV_TRACE_FIELDS,
    M4Store,
    _clean,
    batch_snapshot_to_json,
    command_digest,
    parse_suggestions_csv,
    purchase_order_to_json,
    suggestion_item_to_json,
)

Handler = Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]]

# JSON 导入命令的已知顶层字段（其余输入键不落库不参与摘要，等同旧 pydantic dump）。
_JSON_COMMAND_FIELDS = (
    "suggestions", "tenant_id", "site_id", "tracking_task_id", "idempotency_key",
    "source_module", "procurement_plan_id", "procurement_plan_ids",
    "procurement_plan_version_id", "source_plan_checksum", "order_id", "order_version",
    "project_id", "bom_id", "source_event_id", "observed_at", "actor", "data_scope",
)
# 建议行字段（含 Gate-1 溯源扩展）。
_JSON_ITEM_FIELDS = (
    "item_code", "item_name", "quantity", "unit", "supplier_name", "required_date",
    "project_code", "remark", "order_line_id", "contributing_order_line_ids",
    "contributing_plan_ids", "material_code", "supplier_id",
)

_LEGACY_IMPORT_UNSCOPED_FLAG = "YUNPAI_M4_ALLOW_UNSCOPED_IMPORT"

_EMAIL_CHANNELS = ("email", "wechat", "dingtalk", "feishu", "enterprise_wechat", "sms", "manual")

# 旧 m4 服务同一渠道语气表（deterministic template 直通，B 型降级注记）。
_CHANNEL_TONE: dict[str, tuple[str, str]] = {
    "email": ("邮件", "正式、完整、结构清晰，可以包含称呼、背景、明细列表和结束语。"),
    "wechat": ("微信", "口语化但保持商务礼貌，短句为主，适合直接粘贴到微信聊天。"),
    "dingtalk": ("钉钉", "简洁、工作流风格，重点突出待确认事项，适合群聊或单聊。"),
    "feishu": ("飞书", "清晰、协作化，适合工作即时消息，避免过长寒暄。"),
    "enterprise_wechat": ("企业微信", "商务礼貌、简洁，适合企业微信单聊或群聊。"),
    "sms": ("短信", "极简短，不使用多段格式，保留采购单号、物料摘要、需求日期和回复要求。"),
    "manual": ("手工发送", "通用采购沟通语气，便于采购员复制后自行调整。"),
}


def store_path(ctx: dict[str, Any]) -> str:
    return str((ctx or {}).get("m4_db_path")
               or os.getenv("YUNPAI_M4_DB")
               or os.path.join("runtime", "yunpai-m4.sqlite"))


def _store(ctx: dict[str, Any]) -> M4Store:
    return M4Store(store_path(ctx))


def _tenant(ctx: dict[str, Any]) -> str:
    return str((ctx or {}).get("tenant_id") or "default")


def _task(ctx: dict[str, Any]) -> str:
    return str((ctx or {}).get("task_id") or "")


def _actor(ctx: dict[str, Any]) -> str:
    """操作人：V2 ``tool_context`` 提供 ``actor``（= ``actor_user`` 同源别名）。

    ``actor_user`` 回退只为兼容手工构造 ctx 的调用方（``INFRA-DECISIONS §1.3``）。
    """
    return str((ctx or {}).get("actor") or (ctx or {}).get("actor_user") or "")


def _unscoped_import_enabled() -> bool:
    return os.getenv(_LEGACY_IMPORT_UNSCOPED_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def _resolve_store_row_visibility(row: dict[str, Any] | None, ctx: dict[str, Any],
                                  label: str) -> dict[str, Any]:
    """fail-closed 可见性：受控行必须与 ctx tenant 一致；legacy（NULL）只读可见。"""
    if row is None:
        raise ValueError(f"NOT_FOUND: {label} 不存在")
    if row.get("tenant_id") is not None and row["tenant_id"] != _tenant(ctx):
        raise ValueError(f"NOT_FOUND: {label} 不存在")
    return row


def _json_command(payload: dict[str, Any]) -> dict[str, Any]:
    """取已知顶层字段构造命令（unknown 键忽略，同旧 model_dump 行为）。"""
    return {key: payload.get(key) for key in _JSON_COMMAND_FIELDS if key in payload}


def _json_item_rows(payload_items: list[Any]) -> list[dict[str, Any]]:
    """JSON 建议 → 行文本（旧 json→csv→parse 通道）；material_code/supplier_id 别名回填。"""
    normalized_items = []
    for item in payload_items:
        item = item if isinstance(item, dict) else {}
        alias_item = dict(item)
        if not _clean(alias_item.get("item_code")) and _clean(alias_item.get("material_code")):
            alias_item["item_code"] = alias_item["material_code"]
        if not _clean(alias_item.get("supplier_name")) and _clean(alias_item.get("supplier_id")):
            alias_item["supplier_name"] = alias_item["supplier_id"]
        normalized_items.append(alias_item)

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=[*CSV_EXPECTED_FIELDS, *CSV_TRACE_FIELDS])
    writer.writeheader()
    for item in normalized_items:
        trace_ok = item.get("order_line_id") is not None or (
            item.get("contributing_order_line_ids") is not None
        ) or item.get("contributing_plan_ids") is not None
        writer.writerow({
            **{field: item.get(field) for field in CSV_EXPECTED_FIELDS},
            "order_line_id": item.get("order_line_id"),
            "contributing_order_line_ids": json.dumps(
                item.get("contributing_order_line_ids") or [], ensure_ascii=False
            ) if trace_ok else "",
            "contributing_plan_ids": json.dumps(
                item.get("contributing_plan_ids") or [], ensure_ascii=False
            ) if trace_ok else "",
        })
    return output.getvalue().encode("utf-8")


async def import_m4_purchase_suggestions_json(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """JSON 采购建议导入（等价升级实现；旧 echo 语义已被本实现取代）。

    幂等作用域 (tracking_task_id, idempotency_key)：同正文 replay 返回原批次，
    异正文 IDEMPOTENCY_CONFLICT；不同 tracking_task_id 可复用 key。
    """
    if not isinstance(payload.get("suggestions"), list):
        raise ValueError("VALIDATION_ERROR: suggestions 数组必填")
    command = _json_command(payload)
    ctx_tenant = _tenant(ctx)
    ctx_task = _task(ctx)

    payload_tenant = _clean(command.get("tenant_id"))
    payload_site = _clean(command.get("site_id"))
    if (payload_tenant is None) != (payload_site is None):
        raise ValueError("VALIDATION_ERROR: tenant_id 和 site_id 必须成对提供")
    if payload_tenant is not None and payload_tenant != ctx_tenant:
        raise ValueError(
            f"FORBIDDEN: payload tenant_id（{payload_tenant}）与 ctx tenant_id（{ctx_tenant}）"
            "不一致，禁止越权导入"
        )

    payload_task = _clean(command.get("tracking_task_id"))
    if ctx_task and payload_task and payload_task != ctx_task:
        raise ValueError(
            f"TASK_ID_MISMATCH: tracking_task_id（{payload_task}）与 ctx task_id（{ctx_task}）不一致"
        )
    effective_task = payload_task or (ctx_task or None)
    idempotency_key = _clean(command.get("idempotency_key"))

    if payload_tenant is None:
        # 无租户导入（legacy）：默认关闭；开启时才允许，且不携带受控身份之外的数据。
        if not _unscoped_import_enabled():
            raise ValueError(
                "UNSCOPED_IMPORT_DISABLED: 无租户采购建议导入默认关闭（旧服务同口径）；"
                f"设置 {_LEGACY_IMPORT_UNSCOPED_FLAG}=1 开启，正式交接请走带 tenant/site 的 JSON 导入"
            )
        scope: dict[str, Any] = {"tenant_id": None, "site_id": None}
    else:
        scope = {"tenant_id": payload_tenant, "site_id": payload_site}

    if payload_tenant is None:
        scope["tracking_task_id"] = effective_task
        scope["idempotency_key"] = idempotency_key
    else:
        # 受控导入：受控批次必须有 tracking_task_id（旧 422 语义）。
        if effective_task is None:
            raise ValueError(
                "VALIDATION_ERROR: 受控导入必须提供 tracking_task_id（ctx 或 payload）"
            )
        scope["tracking_task_id"] = effective_task
        scope["idempotency_key"] = idempotency_key

    digest = command_digest(command)
    store = _store(ctx)

    if effective_task and idempotency_key:
        existing = store.get_batch_by_idempotency(effective_task, idempotency_key)
        if existing is not None:
            return _replay_or_conflict(store, existing, digest, effective_task, idempotency_key)

    parsed = parse_suggestions_csv(
        "orchestrator_purchase_suggestions.csv", _json_item_rows(payload["suggestions"])
    )
    try:
        return store.create_batch_with_rows(
            filename="orchestrator_purchase_suggestions.csv",
            parsed=parsed,
            scope=scope,
            command=command,
        )
    except Exception as exc:  # noqa: BLE001 - 唯一约束竞态 → 幂等复核
        if effective_task and idempotency_key and "UNIQUE" in str(exc).upper():
            existing = store.get_batch_by_idempotency(effective_task, idempotency_key)
            if existing is not None:
                return _replay_or_conflict(store, existing, digest, effective_task, idempotency_key)
        raise


def _replay_or_conflict(
    store: M4Store,
    existing: dict[str, Any],
    digest: str,
    tracking_task_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    if existing.get("payload_digest") == digest:
        return {**batch_snapshot_to_json(existing),
                "items": [suggestion_item_to_json(row) for row in store.get_batch_rows(existing["id"])]}
    raise ValueError(
        f"IDEMPOTENCY_CONFLICT: 同 (tracking_task_id={tracking_task_id}, idempotency_key="
        f"{idempotency_key}) 但 payload 正文不同（existing_digest={existing.get('payload_digest')}, "
        f"incoming_digest={digest}）"
    )


async def list_m4_purchase_suggestions(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    page = int(payload.get("page") or 1)
    page_size = int(payload.get("page_size") or 20)
    batch_id = payload.get("batch_id")
    if batch_id is not None:
        batch_id = int(batch_id)
    return _store(ctx).list_suggestions(
        page=page,
        page_size=page_size,
        batch_id=batch_id,
        supplier_name=_clean(payload.get("supplier_name")) or None,
        item_code=_clean(payload.get("item_code")) or None,
        validation_status=_clean(payload.get("validation_status")) or None,
        tenant_id=_tenant(ctx),
    )


def _resolve_generation_scope_and_task(
    store: M4Store, batch: dict[str, Any], payload: dict[str, Any], ctx: dict[str, Any]
) -> tuple[tuple[str | None, str | None], str | None]:
    """批次归属与 TaskID 裁决（manifest 描述 + 旧 0831 路由语义）。

    返回 ((tenant, site), batch_task)；显式作用域必须与批次一致；受控批次
    ctx.task_id 必须与批次 tracking_task_id 完全一致。
    """
    batch_tenant = _clean(batch.get("tenant_id"))
    batch_site = _clean(batch.get("site_id"))
    batch_task = _clean(batch.get("tracking_task_id"))

    payload_tenant = _clean(payload.get("tenant_id"))
    payload_site = _clean(payload.get("site_id"))
    if (payload_tenant is None) != (payload_site is None):
        raise ValueError("VALIDATION_ERROR: tenant_id 和 site_id 必须成对提供")

    ctx_tenant = _tenant(ctx)
    ctx_task = _task(ctx)

    if batch_tenant is None:
        if payload_tenant is not None:
            raise ValueError(
                "FORBIDDEN: 不能为无归属的历史导入批次补写请求作用域（unscoped_import_batch）"
            )
        if batch_task is None and ctx_task:
            raise ValueError(
                "IMPORT_BATCH_TRACKING_IDENTITY_UNAVAILABLE: 历史导入批次没有可验证的"
                "tracking_task_id，拒绝生成采购单（不补造身份）"
            )
        scope = (None, None)
    else:
        if payload_tenant is None:
            scope = (batch_tenant, batch_site)
        else:
            if payload_tenant != batch_tenant:
                raise ValueError(
                    f"FORBIDDEN: 请求 tenant（{payload_tenant}）与导入批次归属（{batch_tenant}）不一致"
                )
            if payload_site != batch_site:
                raise ValueError(
                    f"FORBIDDEN: 请求 site（{payload_site}）与导入批次归属（{batch_site}）不一致"
                )
            scope = (batch_tenant, batch_site)
        if scope[0] != ctx_tenant:
            raise ValueError(
                f"FORBIDDEN: 批次 tenant（{scope[0]}）与 ctx tenant_id（{ctx_tenant}）不一致，"
                "禁止跨租户生成"
            )
    if batch_task:
        if not ctx_task:
            raise ValueError(
                "VALIDATION_ERROR: 已追踪导入批次生成采购单时必须提供 ctx task_id"
            )
        if ctx_task != batch_task:
            raise ValueError(
                f"TASK_ID_MISMATCH: ctx task_id（{ctx_task}）必须与导入批次 tracking_task_id"
                f"（{batch_task}）完全一致"
            )
    return scope, batch_task


async def generate_m4_purchase_orders(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> list[dict[str, Any]]:
    suggestion_ids = payload.get("suggestion_item_ids")
    if not isinstance(suggestion_ids, list) or not suggestion_ids:
        raise ValueError("VALIDATION_ERROR: suggestion_item_ids 数组至少一项")
    store = _store(ctx)
    _, batch = store.resolve_generation_inputs(suggestion_ids)
    scope, batch_task = _resolve_generation_scope_and_task(store, batch, payload, ctx)
    principal = _actor(ctx) or "m4-local"
    return store.generate_purchase_orders(
        suggestion_ids=suggestion_ids,
        scope=scope,
        batch=batch,
        ctx_task_id=batch_task,
        principal=principal,
    )


async def list_m4_purchase_orders(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    return _store(ctx).list_purchase_orders(
        page=int(payload.get("page") or 1),
        page_size=int(payload.get("page_size") or 20),
        status=_clean(payload.get("status")) or None,
        supplier_name=_clean(payload.get("supplier_name")) or None,
        tenant_id=_tenant(ctx),
    )


async def get_m4_purchase_order(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    purchase_order_id = int(payload["purchase_order_id"])
    store = _store(ctx)
    order = _resolve_store_row_visibility(
        store.get_purchase_order(purchase_order_id), ctx, f"采购单 id={purchase_order_id}"
    )
    return purchase_order_to_json(order, store.order_items(purchase_order_id))


async def _submit_or_transition(
    payload: dict[str, Any], ctx: dict[str, Any], target_status: str
) -> dict[str, Any]:
    purchase_order_id = int(payload["purchase_order_id"])
    expected_revision = int(payload["expected_revision"])
    expected_checksum = str(payload["expected_checksum"])
    principal = _actor(ctx) or "m4-local"
    operated_by = _clean(payload.get("operated_by")) or principal
    store = _store(ctx)
    # fail-closed 可见性先行：跨租户/不存在一律 NOT_FOUND；legacy 行显式走
    # LEGACY_UNTRACKED_REVIEW_BLOCKED（由 transition 内裁决）。
    _resolve_store_row_visibility(
        store.get_purchase_order(purchase_order_id), ctx, f"采购单 id={purchase_order_id}"
    )
    return store.transition_order_review(
        purchase_order_id=purchase_order_id,
        ctx_task_id=_task(ctx),
        expected_revision=expected_revision,
        expected_checksum=expected_checksum,
        target_status=target_status,
        principal=principal,
        comment=_clean(payload.get("comment")) or None,
    )


async def submit_m4_purchase_order_review(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """提交人工审核（draft/request_changes → pending_review；绑 TaskID+revision/checksum）。"""
    return await _submit_or_transition(payload, ctx, "pending_review")


async def approve_m4_purchase_order(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """人工批准（pending_review → approved_for_message；仅允许生成邮件草稿）。"""
    return await _submit_or_transition(payload, ctx, "approved_for_message")


async def request_changes_m4_purchase_order(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """人工退回修改（pending_review → request_changes；保留退回原因）。"""
    return await _submit_or_transition(payload, ctx, "request_changes")


# ---------------------------------------------------------------------------
# legacy 发送记录（A-5 边界）：本地耐久出站记录，禁止任何远程转发（P0-5）
# ---------------------------------------------------------------------------


async def send_m4_purchase_order(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """旧版采购单发送记录（LEGACY/LOCAL，A-5 边界=本地耐久出站记录，不实际发送）。

    契约层：``registry-manifests/m4.json#tools[10]`` 已声明 ``deprecated`` +
    ``replaced_by=generate_m4_purchase_inquiry_message`` + ``local_only=true`` +
    ``remote_invocation=forbidden``。本 handler 是该工具唯一的合法执行面：

    - 受控流程（采购单绑定 tracking_task_id）禁止调用；
    - 运行在受控 TaskID 上下文（ctx.task_id 非空）时，即使采购单本身无
      tracking_task_id 也一律拒绝（受控流程不得借用 legacy 通道）；
    - 仅无 TaskID 的 legacy 采购单（旧数据）在 pending_send 时做记录：
      写 m4_outbound_record + PO/行项状态 → sent。不产生到货/库存事实，
      无任何外部网络发送。

    HTTP transport 下的远程拦截在 ``registry.bind_http`` / ``ToolRegistry.call``：
    ``remote_invocation_forbidden()`` 使该工具永不安装 HTTP 适配器，且即使被
    外部塞入远程 handler 也在 ``call()`` 前置拒绝（``REMOTE_INVOCATION_FORBIDDEN``）。
    """
    purchase_order_id = int(payload["purchase_order_id"])
    store = _store(ctx)
    order = _resolve_store_row_visibility(
        store.get_purchase_order(purchase_order_id), ctx, f"采购单 id={purchase_order_id}"
    )
    ctx_task = _task(ctx)
    if order["tracking_task_id"]:
        if not ctx_task or ctx_task != order["tracking_task_id"]:
            raise ValueError(
                f"TASK_ID_MISMATCH: ctx task_id 必须与采购单 tracking_task_id 完全一致"
                f"（ctx={ctx_task or '<empty>'}, order={order['tracking_task_id']}）"
            )
        raise ValueError(
            "LEGACY_SEND_DISABLED: 受控流程禁止旧版 /send（0825 语义）；请使用采购单"
            "审核 + 询价邮件草稿流程，发送边界 A-5 由整合线裁定"
        )
    if ctx_task:
        # 采购单无 TaskID，但调用方处在受控运行上下文 → 仍属受控流程，拒绝。
        raise ValueError(
            f"LEGACY_SEND_DISABLED: 受控流程禁止旧版 /send（ctx task_id={ctx_task}）；"
            "请使用采购单审核 + 询价邮件草稿流程（generate_m4_purchase_inquiry_message）"
        )
    if order["status"] != "pending_send":
        raise ValueError(
            f"INVALID_STATUS: 只有待发送（pending_send）的 legacy 采购单可以记录发送"
            f"（当前 status={order['status']}）"
        )
    items = store.order_items(purchase_order_id)
    note = (
        "A-5 边界：本地耐久出站记录（recorded_only），未实际发送；不产生到货或库存事实。"
        "真实送达语义待用户裁定（R040 A-5）。"
    )
    payload_snapshot = {
        "tenant_id": order.get("tenant_id"),
        "site_id": order.get("site_id"),
        "tracking_task_id": order.get("tracking_task_id"),
        "purchase_order_no": order["purchase_order_no"],
        "supplier_name": order["supplier_name"],
        "required_date": order.get("required_date"),
        "items": [{"item_code": item["item_code"], "item_name": item["item_name"],
                   "quantity": item["quantity"], "unit": item["unit"]} for item in items],
    }
    store.record_outbound(
        purchase_order_id=purchase_order_id,
        kind="purchase_order_send",
        note=note,
        payload=payload_snapshot,
    )
    store.mark_order_and_items_sent(purchase_order_id)
    updated = store.get_purchase_order(purchase_order_id)
    assert updated is not None
    return purchase_order_to_json(updated, store.order_items(purchase_order_id))

# ---------------------------------------------------------------------------
# M4-1b：询价邮件草稿生成（approved_for_message → draft；不发送、无库存事实）
# ---------------------------------------------------------------------------

def _inquiry_message_read(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_id": message["id"],
        "subject": message.get("subject"),
        "content": message["content"],
        "channel": message["channel"],
        "recipient": message.get("recipient"),
        "send_status": message.get("send_status") or "generated",
        "model_name": message.get("model_name"),
        "prompt_version": message.get("prompt_version"),
    }


def _inquiry_recipient(snapshot: dict[str, Any]) -> str | None:
    if not isinstance(snapshot, dict):
        return None
    for key in ("recipient", "email", "to", "address"):
        value = _clean(snapshot.get(key))
        if value:
            return value
    return None


def _fmt_quantity(value: Any) -> str:
    """数量文本（Decimal 规范化 f 格式：100.0 → '100'；50.5 → '50.5'）。"""
    from decimal import Decimal as _Decimal
    from decimal import InvalidOperation as _Invalid

    try:
        return format(_Decimal(str(value)).normalize(), "f")
    except (_Invalid, ValueError, TypeError):
        return str(value)


def _build_inquiry_content(
    order: dict[str, Any],
    items: list[dict[str, Any]],
    channel: str,
    language: str | None,
) -> tuple[str | None, str]:
    """确定性模板草稿（质量对齐旧 mock provider；model_name/prompt_version=None）。

    无模型也可用：固定模板直通（A/B 语义见 s3m4a-notes §6.4）；渠道语气照旧服务。
    """
    tone = _CHANNEL_TONE.get(channel, _CHANNEL_TONE["email"])
    channel_label, tone_instruction = tone
    item_lines = [
        f"{index}. {item['item_name']}（{item['item_code']}），数量 "
        f"{_fmt_quantity(item['quantity'])} {item['unit']}"
        for index, item in enumerate(sorted(items, key=lambda row: row["id"]), start=1)
    ]
    required_date = order.get("required_date") or "请供应商确认最早可交期"
    supplier = order["supplier_name"]
    po_no = order["purchase_order_no"]
    language_hint = "（语言：" + language + "）" if language else ""
    if channel == "sms":
        item_summary = "；".join(
            f"{item['item_name']}{_fmt_quantity(item['quantity'])}{item['unit']}"
            for item in sorted(items, key=lambda row: row["id"])
        )
        content = (
            f"[采购询价] {supplier} 您好，采购单 {po_no} 需求日期 {required_date}，"
            f"物料：{item_summary}。请回复可交期与报价。"
        )
        subject = None
    else:
        content = (
            f"尊敬的 {supplier}：\n\n"
            f"您好！我方采购单 {po_no} 已确认，需求日期为 {required_date}。"
            f"请确认以下物料的报价与最早可交期：\n"
            f"{chr(10).join(item_lines)}\n\n"
            f"{tone_instruction}"
            f"{language_hint}\n"
            f"感谢配合！"
        )
        subject = f"采购询价 {po_no}"
    return subject, content


async def generate_m4_purchase_inquiry_message(
    payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """基于已人工批准采购单生成供应商邮件草稿（M4-1b）。

    只生成草稿：绑定采购单 revision/checksum，保存人工核对收件人/附件快照；
    不对外发送，不产生到货/库存事实；同 PO revision 重放同 payload 幂等。
    """
    purchase_order_id = int(payload["purchase_order_id"])
    expected_revision = int(payload["expected_po_revision"])
    expected_checksum = str(payload["expected_po_checksum"])
    recipient_snapshot = payload.get("recipient_snapshot") or {}
    if not isinstance(recipient_snapshot, dict):
        raise ValueError("VALIDATION_ERROR: recipient_snapshot 必须是对象")
    attachment_snapshot = payload.get("attachment_snapshot") or []
    if not isinstance(attachment_snapshot, list):
        raise ValueError("VALIDATION_ERROR: attachment_snapshot 必须是数组")
    channel = str(payload.get("channel") or "email")
    if channel not in _EMAIL_CHANNELS:
        raise ValueError(f"VALIDATION_ERROR: 不支持的 channel={channel}（{list(_EMAIL_CHANNELS)}）")
    language = _clean(payload.get("language"))

    store = _store(ctx)
    order = _resolve_store_row_visibility(
        store.get_purchase_order(purchase_order_id), ctx, f"采购单 id={purchase_order_id}"
    )
    if not order["tracking_task_id"]:
        raise ValueError(
            "LEGACY_UNTRACKED_MESSAGE_BLOCKED: 无 tracking_task_id 的 legacy 采购单无法"
            "生成受控询价草稿（不补造身份）"
        )
    ctx_task = _task(ctx)
    if not ctx_task or ctx_task != order["tracking_task_id"]:
        raise ValueError(
            f"TASK_ID_MISMATCH: ctx task_id 必须与采购单 tracking_task_id 完全一致"
            f"（ctx={ctx_task or '<empty>'}, order={order['tracking_task_id']}）"
        )
    store.assert_order_revision(order, expected_revision, expected_checksum)
    if order["status"] != "approved_for_message":
        raise ValueError(
            f"INVALID_STATUS: 只有 approved_for_message 采购单可以生成邮件草稿"
            f"（当前 status={order['status']}）"
        )

    items = store.order_items(purchase_order_id)
    existing = store.latest_supplier_message(
        purchase_order_id, order["tracking_task_id"],
        order["current_revision"], order["current_checksum"],
    )
    if existing is not None:
        _assert_message_generation_replay(
            existing,
            channel=channel,
            language=language,
            recipient_snapshot=recipient_snapshot,
            attachment_snapshot=attachment_snapshot,
        )
        return _inquiry_message_read(existing)

    subject, content = _build_inquiry_content(order, items, channel, language)
    principal = _actor(ctx) or "m4-local"
    try:
        message = store.create_supplier_message(
            purchase_order_id=purchase_order_id,
            tracking_task_id=order["tracking_task_id"],
            po_revision=order["current_revision"],
            po_checksum=order["current_checksum"],
            channel=channel,
            subject=subject,
            content=content,
            recipient=_inquiry_recipient(recipient_snapshot),
            recipient_snapshot=recipient_snapshot,
            attachment_snapshot=attachment_snapshot,
            principal=principal,
            model_name=None,
            prompt_version=None,
            language=language,
        )
    except Exception as exc:  # noqa: BLE001 - 唯一索引竞态 → 重放核对
        if "UNIQUE" in str(exc).upper():
            existing = store.latest_supplier_message(
                purchase_order_id, order["tracking_task_id"],
                order["current_revision"], order["current_checksum"],
            )
            if existing is not None:
                _assert_message_generation_replay(
                    existing,
                    channel=channel,
                    language=language,
                    recipient_snapshot=recipient_snapshot,
                    attachment_snapshot=attachment_snapshot,
                )
                return _inquiry_message_read(existing)
        raise
    return _inquiry_message_read(message)


def _assert_message_generation_replay(
    existing: dict[str, Any],
    *,
    channel: str,
    language: str | None,
    recipient_snapshot: dict[str, Any],
    attachment_snapshot: list[Any],
) -> None:
    def _load_json(value: Any, fallback: Any) -> Any:
        if not value:
            return fallback
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return fallback

    recipient_matches = _load_json(
        existing.get("recipient_snapshot"), {}
    ) == recipient_snapshot
    attachment_matches = _load_json(
        existing.get("attachment_snapshot"), []
    ) == attachment_snapshot
    if (
        existing["channel"] != channel
        or existing.get("generation_language") != language
        or not recipient_matches
        or not attachment_matches
    ):
        raise ValueError(
            "IDEMPOTENCY_CONFLICT: 该采购单 revision 已存在不同 payload 的邮件草稿"
            "（channel/language/收件人/附件快照不一致），不覆盖生成"
        )


#: 本地 M4 采购 handler 键集（CSV 变体按 rows-S5.md:66 废弃不搬，故不在本表）。
LOCAL_HANDLERS: dict[str, Handler] = {
    "import_m4_purchase_suggestions_json": import_m4_purchase_suggestions_json,
    "list_m4_purchase_suggestions": list_m4_purchase_suggestions,
    "generate_m4_purchase_orders": generate_m4_purchase_orders,
    "list_m4_purchase_orders": list_m4_purchase_orders,
    "get_m4_purchase_order": get_m4_purchase_order,
    "submit_m4_purchase_order_review": submit_m4_purchase_order_review,
    "approve_m4_purchase_order": approve_m4_purchase_order,
    "request_changes_m4_purchase_order": request_changes_m4_purchase_order,
    "generate_m4_purchase_inquiry_message": generate_m4_purchase_inquiry_message,
    "send_m4_purchase_order": send_m4_purchase_order,
}
