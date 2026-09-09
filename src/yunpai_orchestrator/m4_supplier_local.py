"""M4 供应商主数据与回复解析：供应商 CRUD、回复原文、规则解析、人工确认与事实落库。

迁移来源（只读）：``_wt/INT/src/yunpai_langgraph/m4_supplier_local.py``
（INT 源 sha256 ``da055ac1de21eb177b6a81bb5cd07bfab97e674907396b6a8686405caa71f806``）

- 数据落 ``m4b_store.M4BStore``（env ``YUNPAI_M4B_DB``，表前缀 ``m4b_``）；跨线引用=字符串
  原样、无 FK；供应商主数据 M4 自持 + ``canonical_supplier_code`` 冗余登记。
- 错误风格：``ValueError("CODE: 消息")``（CODE 沿用旧 M4Error 码）。
- 模型登记：``parse_m4_supplier_reply`` = C 型以 B 降级：无模型 → 规则子集
  ``m4-parse-rule-subset.v1`` + ``need_human_review=True`` + ``parse_status=pending_review``
  显式标注，绝不自动回写 tracking。

V2 迁移差异（rows-S5.md）：
- ③ ``_actor`` 兼容 V2 ctx（``actor`` 为 ``actor_user`` 同源别名，``INFRA-DECISIONS §1.2``）；
  V2 无 ``user_id`` 键，保留为最后回退只为兼容手工构造 ctx 的调用方。
- ``ctx.canonical_supplier_code`` V2 不提供（``INFRA-DECISIONS §1.2`` 判「不补」）→ 读取
  保持可选，缺省空串，语义不变。
- ``confirm_m4_supplier_reply`` 的版本顺序约束（先 bump ``parsed_result_version``、再调
  ``confirm_m4_supplier_fact`` 必须重读版本）由本模块 ``reply_set_parse`` 与
  ``confirm_m4_supplier_fact`` 的版本校验共同锁定。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .m4b_store import (
    M4BStore,
    coerce_int,
    decimal_string,
    parse_iso_date,
    parse_received_at,
    payload_checksum,
    utc_now,
)

RULE_SUBSET_PROVIDER = "m4-parse-rule-subset.v1"

_SUPPLIER_STATUSES = {"active", "inactive"}
_SUPPLIER_CHANNELS = {"email", "wechat", "dingtalk", "feishu",
                      "enterprise_wechat", "sms", "manual"}


def _store() -> M4BStore:
    return M4BStore()


def _tenant(ctx: dict[str, Any]) -> str:
    return str(ctx.get("tenant_id") or "default")


def _actor(ctx: dict[str, Any]) -> str:
    """确认人：V2 ``tool_context`` 提供 ``actor``（= ``actor_user`` 同源别名）。"""
    return str(ctx.get("actor") or ctx.get("actor_user") or ctx.get("user_id") or "operator")


def _maybe(value: str | None) -> str | None:
    return value if value not in (None, "") else None


def _email_ok(value: str | None) -> bool:
    if not value:
        return True
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))


def _supplier_read(row: dict[str, Any]) -> dict[str, Any]:
    """manifest supplier item 键集（无 code/category/tax；差异 7-01 登记盘点笔记）。"""
    return {
        "id": int(row["id"]),
        "supplier_name": row["supplier_name"],
        "contact_name": _maybe(row["contact_name"]),
        "email": _maybe(row["email"]),
        "phone": _maybe(row["phone"]),
        "default_channel": row["default_channel"] or "email",
        "remark": _maybe(row["remark"]),
        "status": row["status"] or "active",
    }


async def list_m4_suppliers(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """供应商列表（supplier_name 模糊 + 分页）。"""
    page = max(int(payload.get("page") or 1), 1)
    page_size = max(min(int(payload.get("page_size") or 20), 200), 1)
    supplier_name = str(payload.get("supplier_name") or "") or None
    rows, total = _store().supplier_list(
        _tenant(ctx), supplier_name=supplier_name,
        offset=(page - 1) * page_size, limit=page_size)
    return {"items": [_supplier_read(r) for r in rows], "page": page,
            "page_size": page_size, "total": total}


async def create_m4_supplier(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """新建供应商主数据（重名/渠道/邮箱校验；canonical 冗余登记字段）。"""
    supplier_name = str(payload.get("supplier_name") or "").strip()
    if not supplier_name:
        raise ValueError("VALIDATION_ERROR: create_m4_supplier 需要 supplier_name")
    store = _store()
    tenant = _tenant(ctx)
    if store.supplier_by_name(tenant, supplier_name):
        raise ValueError(f"DUPLICATE_SUPPLIER: 供应商名称已存在: {supplier_name}")
    status = str(payload.get("status") or "active")
    if status not in _SUPPLIER_STATUSES:
        raise ValueError("VALIDATION_ERROR: status 仅支持 active/inactive")
    channel = str(payload.get("default_channel") or "email")
    if channel not in _SUPPLIER_CHANNELS:
        raise ValueError("VALIDATION_ERROR: default_channel 不在支持集合内")
    email = str(payload.get("email") or "").strip()
    if not _email_ok(email):
        raise ValueError(f"VALIDATION_ERROR: 邮箱格式不正确: {email!r}")
    row = store.supplier_create(
        tenant_id=tenant,
        supplier_name=supplier_name,
        contact_name=str(payload.get("contact_name") or "").strip(),
        email=email,
        phone=str(payload.get("phone") or "").strip(),
        default_channel=channel,
        remark=str(payload.get("remark") or "").strip(),
        status=status,
        supplier_code=str(payload.get("supplier_code") or "").strip(),
        # 裁决 2：ctx/批次可解析到 canonical supplier_code 时冗余登记，否则留空。
        # V2 ctx 不提供该键（INFRA-DECISIONS §1.2「不补」）→ 缺省空串，不补造身份。
        canonical_supplier_code=str(ctx.get("canonical_supplier_code") or "").strip(),
    )
    return _supplier_read(row)


async def update_m4_supplier(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """更新供应商主数据（PATCH 语义；只处理 payload 提供的键）。"""
    supplier_id = coerce_int(payload.get("supplier_id"), "supplier_id")
    store = _store()
    tenant = _tenant(ctx)
    current = store.supplier_get(tenant, supplier_id)
    if current is None:
        raise ValueError(f"NOT_FOUND: 供应商不存在: {supplier_id}")
    updatable = ("supplier_name", "contact_name", "email", "phone",
                 "default_channel", "remark", "status")
    fields: dict[str, Any] = {}
    for key in updatable:
        if key not in payload:
            continue
        value = payload[key]
        if key == "supplier_name":
            name = str(value or "").strip()
            if not name:
                raise ValueError("VALIDATION_ERROR: supplier_name 不能为空")
            if store.supplier_by_name(tenant, name, exclude_id=supplier_id):
                raise ValueError(f"DUPLICATE_SUPPLIER: 供应商名称已存在: {name}")
            fields[key] = name
        elif key == "email":
            email = str(value or "").strip()
            if not _email_ok(email):
                raise ValueError(f"VALIDATION_ERROR: 邮箱格式不正确: {email!r}")
            fields[key] = email
        elif key == "status":
            if value not in _SUPPLIER_STATUSES:
                raise ValueError("VALIDATION_ERROR: status 仅支持 active/inactive")
            fields[key] = value
        elif key == "default_channel":
            if value not in _SUPPLIER_CHANNELS:
                raise ValueError("VALIDATION_ERROR: default_channel 不在支持集合内")
            fields[key] = value
        else:
            fields[key] = "" if value is None else str(value).strip()
    if not fields:
        return _supplier_read(current)
    row = store.supplier_update(tenant, supplier_id, fields)
    if row is None:
        raise ValueError(f"NOT_FOUND: 供应商不存在: {supplier_id}")
    return _supplier_read(row)


# ---------------------------------------------------------------------------
# 供应商回复：create → parse（C/B 降级）→ confirm（人工闭合）
# ---------------------------------------------------------------------------

async def create_m4_supplier_reply(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """保存供应商回复原文（字符串引用原样存储；PO 联动校验=集成期增强 7-02）。"""
    purchase_order_id = coerce_int(payload.get("purchase_order_id"), "purchase_order_id")
    purchase_order_no = str(payload.get("purchase_order_no") or "").strip()
    supplier_name = str(payload.get("supplier_name") or "").strip()
    reply_content = str(payload.get("reply_content") or "")
    if not purchase_order_no:
        raise ValueError("VALIDATION_ERROR: purchase_order_no 必填")
    if not supplier_name:
        raise ValueError("VALIDATION_ERROR: supplier_name 必填")
    if not reply_content.strip():
        raise ValueError("VALIDATION_ERROR: reply_content 不能为空")
    received_at = parse_received_at(payload.get("received_at"))
    row = _store().reply_create(
        tenant_id=_tenant(ctx), purchase_order_id=purchase_order_id,
        purchase_order_no=purchase_order_no, supplier_name=supplier_name,
        reply_content=reply_content, received_at=received_at)
    return {
        "id": int(row["id"]),
        "purchase_order_id": int(row["purchase_order_id"]),
        "purchase_order_no": row["purchase_order_no"],
        "supplier_name": row["supplier_name"],
        "reply_content": row["reply_content"],
        "received_at": row["received_at"],
    }


# ---- 确定性规则子集（无模型 B/C 降级） ----

_DATE_RE = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?")
_EXCEPTION_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("quality_hold", ("质量", "检验", "不合格", "冻结", "卡控")),
    ("delayed_delivery", ("无法按期", "不能按期", "不能按时", "延期", "延迟", "推迟",
                          "交期后移", "断料", "缺料", "停产", "不可抗力")),
    ("price_change", ("涨价", "降价", "调价", "报价更新", "价格调整")),
]
_PRICE_PATTERNS = [
    re.compile(r"(?:单价|价格|报价|含税单价|不含税单价)[:：为是]?\s*[¥￥]?\s*([0-9]+(?:\.[0-9]{1,4})?)"),
    re.compile(r"[¥￥]\s*([0-9]+(?:\.[0-9]{1,4})?)"),
    re.compile(r"\b(?:CNY|RMB|USD|EUR)\s*([0-9]+(?:\.[0-9]{1,4})?)\b"),
    re.compile(r"\b([0-9]+(?:\.[0-9]{1,4})?)\s*(?:元|人民币|美元|欧元)\b"),
]


def rule_parse_reply(text: str) -> dict[str, Any]:
    """确定性规则解析子集：只提取高置信句法信号；其余留空交给人工确认。

    返回键与 manifest parse 输出一致；need_human_review 恒 True（B 型降级显式标注）。
    """
    content = text or ""
    delivery_date: str | None = None
    date_matches: list[tuple[int, int, int]] = []
    for match in _DATE_RE.finditer(content):
        year, month, day = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        try:
            parsed = date(year, month, day)
        except ValueError:
            continue
        date_matches.append((match.start(), parsed))
    if date_matches:
        # 规则：取首个合法日期之后的「最早未来日期」，无未来则取首个合法日期。
        today = date.today()
        future = [parsed for _, parsed in date_matches if parsed >= today]
        picked = min(future) if future else date_matches[0][1]
        delivery_date = picked.isoformat()

    currency = "CNY"
    currency_hits: list[tuple[int, str]] = []
    for token, code in (("美元", "USD"), ("USD", "USD"), ("US$", "USD"),
                        ("欧元", "EUR"), ("EUR", "EUR"), ("€", "EUR"),
                        ("人民币", "CNY"), ("RMB", "CNY"), ("CNY", "CNY"), ("¥", "CNY"),
                        ("￥", "CNY"), ("元", "CNY")):
        position = content.find(token)
        if position >= 0:
            currency_hits.append((position, code))
    if currency_hits:
        currency = min(currency_hits, key=lambda item: item[0])[1]

    unit_price: str | None = None
    for pattern in _PRICE_PATTERNS:
        match = pattern.search(content)
        if match:
            raw = match.group(1)
            try:
                unit_price = decimal_string(raw)
            except ValueError:
                continue
            if unit_price is not None:
                break

    tax_included: bool | None = None
    if "含税" in content and "不含税" not in content:
        tax_included = True
    elif "不含税" in content or "未税" in content or "不含税价" in content:
        tax_included = False

    exception_type: str | None = None
    exception_description: str | None = None
    for etype, keywords in _EXCEPTION_RULES:
        for keyword in keywords:
            position = content.find(keyword)
            if position >= 0:
                exception_type = etype
                start = max(position - 30, 0)
                excerpt = content[start:position + 90].replace("\n", " ").strip()
                exception_description = excerpt[:120] or keyword
                break
        if exception_type:
            break

    found = sum(1 for field in (delivery_date, unit_price, tax_included, exception_type)
                if field not in (None,))
    confidence = 0.30 if found >= 3 else (0.20 if found >= 1 else 0.10)
    return {
        "delivery_date": delivery_date,
        "unit_price": unit_price,
        "currency": currency,
        "tax_included": tax_included,
        "exception_type": exception_type,
        "exception_description": exception_description,
        "confidence": confidence,
        "need_human_review": True,
    }


def _parse_result_read(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "delivery_date": result.get("delivery_date"),
        "unit_price": result.get("unit_price"),
        "currency": result.get("currency") or "CNY",
        "tax_included": result.get("tax_included"),
        "exception_type": result.get("exception_type"),
        "exception_description": result.get("exception_description"),
        "confidence": float(result.get("confidence") or 0.0),
        "need_human_review": bool(result.get("need_human_review")),
    }


async def parse_m4_supplier_reply(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """解析供应商回复（C 型缺模型 → B 降级规则子集 + need_human_review=True 显式标注）。

    降级路径断言：confidence<0.5、need_human_review=True、parse_status=pending_review、
    parse_provider=m4-parse-rule-subset.v1、不自动写 tracking（R045）。
    """
    reply_id = coerce_int(payload.get("reply_id"), "reply_id")
    store = _store()
    tenant = _tenant(ctx)
    reply = store.reply_get(tenant, reply_id)
    if reply is None:
        raise ValueError(f"NOT_FOUND: 供应商回复不存在: {reply_id}")
    result = rule_parse_reply(reply["reply_content"])
    # 无模型显式降级：模型可用路径=外部解析候选 + 人工/高置信自动确认（未来 Provider 注入）。
    store.reply_set_parse(
        tenant, reply_id,
        parsed_result=result,
        parsed_result_version=int(reply["parsed_result_version"]) + 1,
        confidence=float(result["confidence"]),
        need_human_review=True,
        parse_status="pending_review",
        parse_provider=RULE_SUBSET_PROVIDER,
        model_name="",
        prompt_version=RULE_SUBSET_PROVIDER,
    )
    return _parse_result_read(result)


def _bool_value(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).strip().lower() in {"true", "1", "yes"}:
        return True
    if str(value).strip().lower() in {"false", "0", "no"}:
        return False
    raise ValueError(f"VALIDATION_ERROR: {field} 必须为布尔值")


def _confirmed_result(payload: dict[str, Any], *, confirmed: bool) -> dict[str, Any]:
    """人工确认结果规范化（manifest confirm 输出键集）。"""
    confidence = payload.get("confidence")
    try:
        confidence = float(1.0 if confidence is None else confidence)
    except (TypeError, ValueError) as exc:
        raise ValueError("VALIDATION_ERROR: confidence 必须为 0..1 数值") from exc
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("VALIDATION_ERROR: confidence 必须为 0..1 数值")
    tax_included = payload.get("tax_included")
    if tax_included not in (None, ""):
        tax_included = _bool_value(tax_included, "tax_included")
    else:
        tax_included = None
    return {
        "delivery_date": parse_iso_date(payload.get("delivery_date")),
        "unit_price": decimal_string(payload.get("unit_price")),
        "currency": str(payload.get("currency") or "CNY"),
        "tax_included": tax_included,
        "exception_type": _maybe(str(payload.get("exception_type") or "")),
        "exception_description": _maybe(str(payload.get("exception_description") or "")),
        "confidence": confidence,
        "need_human_review": not confirmed,
    }


async def confirm_m4_supplier_reply(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """人工确认/修正解析结果，闭合 HITL；确认后才作为正式承诺写入追踪。

    契约层：``registry-manifests/m4.json#tools[13]`` 已声明 ``deprecated`` +
    ``replaced_by=confirm_m4_supplier_fact``。**主用**行级 ``confirm_m4_supplier_fact``
    （绑定 purchase_order_item_id + supplier_reply_version + checksum，可审计）；
    本工具是 PO 级便捷封装，保留兼容返回结构。

    顺序约束：本工具会 bump ``parsed_result_version``（下方 ``reply_set_parse``），
    因此先调用本工具、再调用 ``confirm_m4_supplier_fact`` 时，必须重新读取回复版本
    后再传 ``supplier_reply_version``；沿用旧版本会触发 ``IDEMPOTENCY_CONFLICT``。

    幂等/防重复确认：已 confirmed 且与上版结果完全一致 → 原样 replay 不 bump 版本
    （差异 7-09）；need_human_review=true → 只保存修正草稿（pending_review，不写追踪）。
    """
    reply_id = coerce_int(payload.get("reply_id"), "reply_id")
    still_review = _bool_value(payload.get("need_human_review", False), "need_human_review")
    store = _store()
    tenant = _tenant(ctx)
    reply = store.reply_get(tenant, reply_id)
    if reply is None:
        raise ValueError(f"NOT_FOUND: 供应商回复不存在: {reply_id}")
    result = _confirmed_result(payload, confirmed=not still_review)
    prior = _parse_result_read(_parsed_or_empty(reply))
    if not still_review and reply["parse_status"] == "confirmed" and result == prior:
        # 幂等 replay：同一结果重复确认不产生新版本/重复写。
        return prior
    status = "confirmed" if not still_review else "pending_review"
    store.reply_set_parse(
        tenant, reply_id,
        parsed_result=result,
        parsed_result_version=int(reply["parsed_result_version"]) + 1,
        confidence=float(result["confidence"]),
        need_human_review=still_review,
        parse_status=status,
        parse_provider="human-confirm" if not still_review else "human-draft",
        model_name="",
        prompt_version="",
    )
    if not still_review:
        _apply_confirmed_to_tracking(tenant, reply, result)
    return result


def _parsed_or_empty(reply: dict[str, Any]) -> dict[str, Any]:
    import json

    text = reply.get("parsed_result_json") or ""
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _apply_confirmed_to_tracking(tenant: str, reply: dict[str, Any],
                                 result: dict[str, Any]) -> None:
    """正式承诺 → 追踪（PO 级行；行级由 confirm_m4_supplier_fact 补充）。

    旧语义按 PO 每行回写；本线无 PO 行注册表 → po 级行 keyed by (po_id, item='')，
    差异 7-04/7-05 登记盘点笔记（M4A join 后按行展开）。
    """
    promised_date = result.get("delivery_date")
    overdue = False
    if promised_date:
        try:
            overdue = date.fromisoformat(promised_date) < date.today()
        except ValueError:
            overdue = False
    _store().tracking_upsert(
        tenant_id=tenant,
        purchase_order_id=str(reply["purchase_order_id"]),
        purchase_order_no=reply["purchase_order_no"],
        purchase_order_item_id="",
        supplier_name=reply["supplier_name"],
        promised_date=promised_date,
        unit_price=result.get("unit_price"),
        currency=result.get("currency"),
        exception_type=result.get("exception_type"),
        exception_description=result.get("exception_description"),
        arrival_status="not_received",
        is_overdue=overdue,
    )


# ---------------------------------------------------------------------------
# confirm_m4_supplier_fact（行级事实确认，只信 ctx 确认人/时间）
# ---------------------------------------------------------------------------

async def confirm_m4_supplier_fact(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """人工确认回复当前解析版本 → 行级供应事实（幂等 replay / 版本冲突 409 语义）。

    调用方不能提供确认人/时间：confirmed_by=ctx actor、confirmed_at=服务端 UTC；
    重复确认返回原结果；同一回复版本不同事实 → IDEMPOTENCY_CONFLICT。
    输出形状=urn m4.supplier-fact-confirmation.v1（additionalProperties:false）。
    """
    if payload.get("schema_version") != "m4.supplier-fact-confirmation-request.v1":
        raise ValueError(
            "VALIDATION_ERROR: schema_version 必须为 m4.supplier-fact-confirmation-request.v1")
    reply_id = coerce_int(payload.get("reply_id"), "reply_id")
    item_id = coerce_int(payload.get("purchase_order_item_id"), "purchase_order_item_id")
    reply_version = coerce_int(payload.get("supplier_reply_version"), "supplier_reply_version")
    raw_result = payload.get("result") or {}
    if not isinstance(raw_result, dict):
        raise ValueError("VALIDATION_ERROR: result 必须为对象")
    allowed = ("delivery_date", "exception_type", "exception_description")
    result = {key: raw_result.get(key) for key in allowed if key in raw_result}
    delivery_date = parse_iso_date(result.get("delivery_date"))
    exception_type = _maybe(str(result.get("exception_type") or ""))
    exception_description = _maybe(str(result.get("exception_description") or ""))
    if delivery_date is None and exception_type is None:
        raise ValueError(
            "VALIDATION_ERROR: result 至少需要非空 delivery_date 或 exception_type")

    store = _store()
    tenant = _tenant(ctx)
    reply = store.reply_get(tenant, reply_id)
    if reply is None:
        raise ValueError("NOT_FOUND: 供应商确认资源不存在")
    if not (reply["parsed_result_json"] or "") or int(reply["parsed_result_version"]) != reply_version:
        raise ValueError(
            "IDEMPOTENCY_CONFLICT: supplier reply version is not available for confirmation: "
            f"supplier_reply_version_conflict (current={reply['parsed_result_version']}, "
            f"requested={reply_version})")

    parse_confidence = _parse_confidence_decimal(reply.get("confidence"))
    result_payload = {"delivery_date": delivery_date,
                      "exception_type": exception_type,
                      "exception_description": exception_description}
    confirmed_by = _actor(ctx)
    confirmed_at = utc_now()
    confirmation_payload = {
        "supplier_reply_id": reply_id,
        "supplier_reply_version": reply_version,
        "purchase_order_item_id": item_id,
        "confirmed_by": confirmed_by,
        "confirmed_at": confirmed_at,
        "parse_confidence": parse_confidence,
        "result": result_payload,
    }
    checksum = payload_checksum(confirmation_payload)

    existing = store.fact_find(tenant, item_id=str(item_id), reply_id=reply_id,
                               reply_version=reply_version)
    if existing is not None:
        return _fact_replay_or_conflict(existing, reply_id, reply_version, item_id,
                                        parse_confidence, result_payload)
    row = store.fact_create(
        tenant_id=tenant, item_id=str(item_id), reply_id=reply_id, reply_version=reply_version,
        confirmed_by=confirmed_by, confirmed_at=confirmed_at,
        parse_confidence=parse_confidence, delivery_date=delivery_date,
        exception_type=exception_type, exception_description=exception_description,
        result_payload=result_payload, checksum=checksum)
    return _fact_read(row)


def _parse_confidence_decimal(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return decimal_string(value)


def _fact_replay_or_conflict(existing: dict[str, Any], reply_id: int, reply_version: int,
                             item_id: int, parse_confidence: str | None,
                             result_payload: dict[str, Any]) -> dict[str, Any]:
    """重复确认：结果与原确认一致 → 返回原记录；不一致 → 409 冲突语义。"""
    replay_payload = {
        "supplier_reply_id": reply_id,
        "supplier_reply_version": reply_version,
        "purchase_order_item_id": item_id,
        "confirmed_by": existing["confirmed_by"],
        "confirmed_at": existing["confirmed_at"],
        "parse_confidence": parse_confidence,
        "result": result_payload,
    }
    if existing["checksum"] == payload_checksum(replay_payload):
        return _fact_read(existing)
    raise ValueError(
        "IDEMPOTENCY_CONFLICT: supplier fact confirmation already exists for this reply "
        "version: confirmation_conflict")


def _fact_read(row: dict[str, Any]) -> dict[str, Any]:
    import json

    return {
        "schema_version": "m4.supplier-fact-confirmation.v1",
        "id": int(row["id"]),
        "purchase_order_item_id": int(row["purchase_order_item_id"]),
        "supplier_reply_id": int(row["supplier_reply_id"]),
        "supplier_reply_version": int(row["supplier_reply_version"]),
        "confirmed_by": row["confirmed_by"],
        "confirmed_at": row["confirmed_at"],
        "parse_confidence": row.get("parse_confidence"),
        "delivery_date": row.get("delivery_date"),
        "exception_type": row.get("exception_type"),
        "exception_description": row.get("exception_description"),
        "result_payload": json.loads(row["result_payload_json"]),
        "checksum": row["checksum"],
    }


LOCAL_HANDLERS: dict[str, Any] = {
    "list_m4_suppliers": list_m4_suppliers,
    "create_m4_supplier": create_m4_supplier,
    "update_m4_supplier": update_m4_supplier,
    "create_m4_supplier_reply": create_m4_supplier_reply,
    "parse_m4_supplier_reply": parse_m4_supplier_reply,
    "confirm_m4_supplier_reply": confirm_m4_supplier_reply,
    "confirm_m4_supplier_fact": confirm_m4_supplier_fact,
}
