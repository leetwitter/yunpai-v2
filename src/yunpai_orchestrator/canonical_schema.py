"""Canonical 字段单一事实源：entity_type → 字段 / 必填 / 类型。

这是「我们自己的数据库格式」的唯一权威定义。agent 的 map_to_canonical 输出、
确定性校验器、写库分发，都只对着这里。字段名是我们的 canonical 字段，
不是上传文件的原始列名。

原则（见 HISTORY.md「Agent 自由化原则」一节）：
- agent 自由理解「分类 + 字段映射 + 组合」；
- 确定性校验「字段名合法 / 必填齐全 / 类型正确 / 数值照抄」；
- 事实数值不得由 agent 编造（agent 只负责指出是哪一列，值从文件照抄）。
"""
from __future__ import annotations

from typing import Any

# entity_type -> {required: 必填字段, fields: 允许出现的字段}
CANONICAL_SCHEMA: dict[str, dict[str, Any]] = {
    "order": {
        # due_date 很关键，但不少来源（如订单产量工资表）不写交期；识别层不做硬拦，
        # 缺交期在 M3/MRP 需要时由下游 BLOCKED_INPUT Gate 兜底。
        "required": ("order_id", "quantity"),
        "fields": (
            "order_id", "product_code", "product_name", "quantity", "due_date",
            "customer_name", "unit_price", "total_amount",
        ),
    },
    "product": {
        "required": ("product_code", "product_name"),
        "fields": ("product_code", "product_name", "model", "aliases", "version"),
    },
    "bom": {
        # 产品身份以 product_code（型号，如 W-H913）为准；extract_bom_full 会从
        # BOM 的「成品成本」区块抽出型号码。
        "required": ("product_code", "lines"),
        "fields": ("product_code", "product_name", "version", "lines"),
    },
    "material": {
        "required": ("material_code", "material_name"),
        "fields": ("material_code", "material_name", "specification", "unit", "aliases"),
    },
    "equipment": {
        "required": ("equipment_code", "equipment_name"),
        "fields": ("equipment_code", "equipment_name", "equipment_type", "model", "line", "status", "capability_codes"),
    },
    "station": {
        "required": ("station_code",),
        "fields": ("station_code", "station_name", "operation_code", "line"),
    },
    "worker": {
        "required": ("worker_code", "worker_name"),
        "fields": ("worker_code", "worker_name", "skill", "shift", "status"),
    },
    "inventory": {
        "required": ("material_code", "available_qty"),
        "fields": ("material_code", "material_name", "warehouse", "available_qty", "unit", "lot_no"),
    },
    "supplier": {
        "required": ("supplier_code", "supplier_name"),
        "fields": ("supplier_code", "supplier_name", "legal_id", "status", "material_codes"),
    },
    "tooling": {
        "required": ("tooling_code", "tooling_name"),
        "fields": ("tooling_code", "tooling_name", "tooling_type", "model", "status"),
    },
    "route": {
        "required": ("route_code", "operations"),
        "fields": ("route_code", "route_version", "product_code", "operations"),
    },
    "operation": {
        "required": ("operation_code", "operation_name"),
        "fields": ("operation_code", "operation_name", "sequence", "standard_minutes"),
    },
    "calendar": {
        "required": ("calendar_date", "shift"),
        "fields": ("calendar_date", "shift", "start_time", "end_time"),
    },
    "production_daily_report": {
        # 生产日报（如押出部日报：日期/订单编号/产品型号/数量米/合计米/备注）
        "required": ("date", "quantity"),
        "fields": ("date", "order_id", "product_code", "product_name", "quantity", "total_quantity", "remark"),
    },
    "document": {
        "required": ("role", "title"),
        "fields": ("role", "title", "product_codes", "content_uri", "route_steps", "document_no", "revision"),
    },
}

# 事实数值字段：必须是 number（agent 照抄，不允许字符串化）。
NUMERIC_FIELDS = frozenset({
    "quantity", "available_qty", "unit_price", "total_amount", "total_quantity",
    "standard_minutes", "sequence",
})

# 这些字段是列表/嵌套结构，01 只做浅层类型校验（是 list 即可）。
LIST_FIELDS = frozenset({
    "lines", "operations", "aliases", "material_codes", "capability_codes",
    "route_steps", "product_codes",
})

# 内部/证据字段：agent 输出里允许携带，但会被剥离成 evidence，不写进 payload。
EVIDENCE_FIELDS = frozenset({"_source", "_row", "_page", "_image", "_confidence"})


def known_entity_types() -> tuple[str, ...]:
    return tuple(CANONICAL_SCHEMA)


def required_fields(entity_type: str) -> tuple[str, ...]:
    schema = CANONICAL_SCHEMA.get(entity_type)
    return tuple(schema["required"]) if schema else ()


def allowed_fields(entity_type: str) -> frozenset[str]:
    schema = CANONICAL_SCHEMA.get(entity_type)
    return frozenset(schema["fields"]) if schema else frozenset()


def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_canonical(entity_type: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    """校验 agent 输出的 canonical 记录，返回 {clean_records, errors, evidence}。

    - 字段名必须在 allowed_fields 内（证据字段 _source/_row 等被剥离）；
    - 必填字段必须存在且非空；
    - 数值字段必须是 number；
    - 每条通过校验的记录，其 _source/_row 等证据字段被抽到 evidence。
    """
    errors: list[dict[str, Any]] = []
    clean_records: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    if entity_type not in CANONICAL_SCHEMA:
        return {"clean_records": [], "errors": [{"code": "UNKNOWN_ENTITY_TYPE", "message": f"未知 entity_type: {entity_type}"}], "evidence": []}
    if not isinstance(records, list) or not records:
        return {"clean_records": [], "errors": [{"code": "EMPTY_RECORDS", "message": "records 必须为非空数组"}], "evidence": []}
    schema = CANONICAL_SCHEMA[entity_type]
    allowed = frozenset(schema["fields"])
    required = tuple(schema["required"])
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            errors.append({"code": "INVALID_RECORD", "message": f"records[{index}] 必须是对象", "record_index": index})
            continue
        record_errors: list[str] = []
        for key in record:
            if key in EVIDENCE_FIELDS:
                continue
            if key not in allowed:
                record_errors.append(f"字段 '{key}' 不在 {entity_type} 允许集合内")
        for field in required:
            value = record.get(field)
            if value in (None, "", [], {}):
                record_errors.append(f"缺少必填字段 '{field}'")
        for key, value in record.items():
            if key in NUMERIC_FIELDS and value is not None and value != "" and not _is_numeric(value):
                record_errors.append(f"字段 '{key}' 必须是数字，得到 {type(value).__name__}")
            if key in LIST_FIELDS and value is not None and not isinstance(value, (list, str)):
                record_errors.append(f"字段 '{key}' 必须是数组或字符串")
        if record_errors:
            errors.append({"code": "INVALID_RECORD", "message": "; ".join(record_errors), "record_index": index})
            continue
        source = {}
        for key in EVIDENCE_FIELDS:
            if key not in record or record[key] in (None, ""):
                continue
            value = record[key]
            if key == "_source" and isinstance(value, dict):
                source.update(value)
            else:
                source[key] = value
        # 归一化：LIST_FIELDS 的单个字符串值 → [字符串]，避免 agent 输出单值时被误拒。
        clean: dict[str, Any] = {}
        for key, value in record.items():
            if key in EVIDENCE_FIELDS:
                continue
            if key in LIST_FIELDS and isinstance(value, str):
                clean[key] = [value]
            else:
                clean[key] = value
        clean_records.append(clean)
        if source:
            evidence.append({"record_index": index, **source})
    return {"clean_records": clean_records, "errors": errors, "evidence": evidence}
