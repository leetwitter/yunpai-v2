"""M3 物料计划本地实现：订单级齐套快照（canonical order/bom/inventory 确定性计算 + 内容寻址持久化）。

迁移来源（只读）：``_wt/INT/src/yunpai_langgraph/m3_local.py``

- sha256: ``e5ae0ac7c97abb9657ea5306966772c3ca9634dc1509545d1ad492851d1e4204``
- 规模: 56886 字节 / 1231 行
- 归属分片: **M3**（逐工具结论见 ``_migration/rows-S4.md``）

**本文件按 rows-S4 的「保留HTTP / 不搬」裁定只迁入被判定「改造后搬」的部分**：

- ``get_material_readiness_snapshot``（正式实现：INT :625-671 + ``_compute_snapshot`` :291-570）
  —— V2 无本地 handler，迁入后登记进 ``workers.HANDLERS``；
- ``m2_package_to_order_bom`` / ``open_po_quantities``（INT :1122-1211）—— 供 ``workers.m3_mrp``
  升级为集成版（R4-REQ-1/2）使用。

**未迁入（有意，不是遗漏）**：其余 12 个 LEGACY 工具（``list_m3_orders``/``get_m3_order``/
``get_m3_procurement_plan``/``get_persisted_m3_plan``/``get_material_readiness``/``get_pr_po_drafts``/
``get_m3_approval_tasks``/4 个审批动作/``get_m3_m4_handoffs``/``export_m3_procurement_suggestions``）
与 ``run_mrp_procurement_plan`` 判「保留HTTP / 废弃」——V2 侧它们由 M3 HTTP 服务承接，搬本地实现
会**遮蔽 HTTP 绑定**（``binding.py:49-52`` 本地优先）且本地实现恒失败/恒空或读错事实源；
``receive_m3_material_demand`` 属 ``ORCHESTRATION_INTERNAL``（不进路由目录，且禁止登记进
``HANDLERS``，见 INFRA-DECISIONS §3.3）。

依赖核对：``.m3_store``（M-INFRA 完整迁入）、``.m0_backend.M0Store``（V2 存在，``list_entities``
返回 ``{entities:[...]}``）、``.m3_m4_tooling.M3_LEGACY_DECISIONS``（本分片在 V2 版上增量补齐）。
ctx：``m3_db_path`` V2 不提供 → 回退 ``YUNPAI_M3_DB``（INFRA-DECISIONS §1.3）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from .fact_gateway import split_envelope
from .m3_m4_tooling import M3_LEGACY_DECISIONS
from .m3_store import M3Store

# ---------------------------------------------------------------------------
# module-level facts
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "m5.material-readiness-snapshot.v1"
CALCULATION_VERSION = "m3.material-readiness.local.v1"

# qc_status values treated as allocatable stock; anything else (including
# quarantine/blocked/qa_hold/…) is excluded from allocation and, when it is
# the only evidence for a demanded material, drives quality_hold.
_RELEASED_QC = {"released", "ok", "pass", "approved", "合格", "released_ok"}

_EPS = 1e-9


# ---------------------------------------------------------------------------
# small helpers (mirroring m5_tools/workers conventions, kept local)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _trace(ctx: dict[str, Any], suffix: str) -> str:
    return f"{ctx.get('task_id', 'local')}:m3-{suffix}"


def _err(code: str, message: str) -> dict[str, Any]:
    return {"code": code, "message": message, "details": []}


def _num(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _num_strict(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_qty(value: float) -> str:
    return f"{value:.10g}"


def _sha256_text(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _store_path(ctx: dict[str, Any]) -> str:
    """M3Store db path: ctx override > YUNPAI_M3_DB > default (as m5_tools)."""
    return str((ctx or {}).get("m3_db_path") or os.getenv("YUNPAI_M3_DB") or "runtime/yunpai-m3.sqlite")


def _canonical_path() -> str | None:
    """Canonical store is only consulted when YUNPAI_M0_DB points to an
    existing file (same policy as m0_facts: no implicit big-db reads)."""
    path = os.getenv("YUNPAI_M0_DB") or ""
    return path if path and os.path.exists(path) else None


def _canonical_entities(entity_type: str, tenant_id: str) -> list[dict[str, Any]] | None:
    """Returns entity rows [{entity_id, canonical_key, version, payload_json
    (parsed record), checksum}] or None when the canonical read itself fails
    (caller must treat None as "facts unavailable", [] as "no facts").

    读口统一走 ``M0Store.list_entities``（不直连 sqlite；父会话 M0 读口一致性裁决）。
    """
    path = _canonical_path()
    if not path:
        return None
    from .m0_backend import M0Store  # local import keeps module import cheap

    try:
        result = M0Store(path).list_entities(entity_type, tenant_id=tenant_id)
    except Exception:
        return None
    return list(result.get("entities", []))


def _unpack(record: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(payload, identity) split of a canonical record envelope; tolerates
    flat records too (same policy as m0_facts._envelope_fields).

    形状归一（``payload.attributes`` 并入顶层、**顶层优先**）统一由
    ``fact_gateway.split_envelope`` 实现；M3 原口径额外删除合并后的
    ``attributes`` 键（``drop_attributes=True``），不新增事实。
    """
    return split_envelope(record, drop_attributes=True)


def _business_key(entity: dict[str, Any], identity: dict[str, Any], payload: dict[str, Any],
                  field: str) -> str:
    return str(identity.get("business_key") or entity.get("canonical_key") or payload.get(field) or "")


# ---------------------------------------------------------------------------
# canonical fact extraction (read-only, per tenant)
# ---------------------------------------------------------------------------

def _find_order(order_id: str, tenant_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Locate the canonical order entity for order_id.  Returns (entity, notes)."""
    wanted = str(order_id or "").strip()
    if not wanted:
        return None, ["empty order_id"]
    entities = _canonical_entities("order", tenant_id)
    if entities is None:
        return None, ["canonical_store_unavailable"]
    for entity in entities:
        payload, identity = _unpack(entity.get("payload_json") or {})
        key = str(entity.get("canonical_key") or "")
        candidates = [key, str(identity.get("business_key") or ""),
                      str(payload.get("order_id") or ""), str(payload.get("order_number") or ""),
                      str(payload.get("order_no") or "")]
        if wanted in {c for c in candidates if c}:
            return {"entity": entity, "payload": payload, "identity": identity}, []
    return None, [f"order_not_found:{wanted}"]


def _order_demand_products(order: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Products with ordered quantities derived from a canonical order record.

    Each product: {product_code, product_name, qty, uom}.  Order lines win
    when they carry product codes; otherwise the header product is used.
    """
    if order is None:
        return [], ["order_missing"]
    payload, identity = order["payload"], order["identity"]
    notes: list[str] = []
    lines = payload.get("lines")
    line_products: list[dict[str, Any]] = []
    if isinstance(lines, list):
        for line in lines:
            if not isinstance(line, dict):
                continue
            code = str(line.get("product_code") or line.get("product") or "").strip()
            if not code:
                continue
            qty = _num_strict(line.get("quantity") if line.get("quantity") not in (None, "") else line.get("qty"))
            if qty is None:
                notes.append(f"line_quantity_invalid:{code}")
                qty = 0.0
            line_products.append({
                "product_code": code,
                "product_name": str(line.get("product_name") or line.get("name") or code),
                "qty": qty,
                "uom": str(line.get("uom") or line.get("unit") or ""),
            })
    if line_products:
        return line_products, notes
    code = str(payload.get("product_code") or identity.get("business_key") or "").strip()
    if not code:
        return [], notes + ["order_facts_incomplete:no_product_code"]
    qty = _num_strict(payload.get("order_qty") if payload.get("order_qty") not in (None, "") else payload.get("quantity"))
    if qty is None:
        qty = _num_strict(payload.get("qty"))
    if qty is None:
        return [], notes + ["order_facts_incomplete:quantity_invalid"]
    return [{
        "product_code": code,
        "product_name": str(payload.get("product_name") or code),
        "qty": qty,
        "uom": str(payload.get("uom") or payload.get("unit") or ""),
    }], notes


def _bom_for_product(product_code: str, tenant_id: str,
                     boms: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """BOM demand rows for a product from preloaded canonical bom entities.

    Rows: {material_code, material_name, qty_per, uom, loss_rate}.
    Returns (rows, bom_meta); bom_meta is None when no matching BOM exists,
    otherwise carries {bom_key, version_id, payload_json} evidence.
    """
    wanted = str(product_code or "").strip()
    for entity in boms:
        payload, identity = _unpack(entity.get("payload_json") or {})
        codes = {str(identity.get("business_key") or ""), str(payload.get("product_code") or "")}
        if wanted not in {c for c in codes if c}:
            continue
        lines = payload.get("lines")
        meta = {
            "bom_key": str(entity.get("canonical_key") or ""),
            "version_id": str(identity.get("version_id") or entity.get("version") or ""),
            "payload_json": entity.get("payload_json") or {},
            "empty_lines": True,
        }
        if not isinstance(lines, list) or not lines:
            return [], meta
        rows: list[dict[str, Any]] = []
        for line in lines:
            if not isinstance(line, dict):
                continue
            code = str(line.get("material_code") or "").strip()
            if not code:
                continue
            qty_per = _num_strict(line.get("qty_per") if line.get("qty_per") not in (None, "") else line.get("quantity_per"))
            if qty_per is None:
                qty_per = _num_strict(line.get("quantity"))
            if qty_per is None:
                qty_per = 0.0
            loss = _num(line.get("loss_rate"), 0.0)
            rows.append({
                "material_code": code,
                "material_name": str(line.get("material_name") or line.get("name") or code),
                "qty_per": qty_per,
                "uom": str(line.get("uom") or line.get("unit") or ""),
                "loss_rate": loss,
            })
        meta["empty_lines"] = False
        return rows, meta
    return [], None


def _inventory_lots(tenant_id: str) -> list[dict[str, Any]] | None:
    """Inventory lot entities of a tenant.  Returns None only when the
    canonical read itself failed (vs [] for an empty/quarantined store)."""
    entities = _canonical_entities("inventory", tenant_id)
    if entities is None:
        return None
    lots: list[dict[str, Any]] = []
    for entity in entities:
        payload, identity = _unpack(entity.get("payload_json") or {})
        code = str(identity.get("business_key") or payload.get("material_code") or "").strip()
        if not code:
            continue
        lots.append({
            "material_code": code,
            "warehouse": str(payload.get("warehouse") or ""),
            "lot_no": str(payload.get("lot_no") or ""),
            "qc_status": str(payload.get("qc_status") or ""),
            "available_qty": _num(payload.get("available_qty"), 0.0),
            "locked_qty": _num(payload.get("locked_qty"), 0.0),
            "received_at": str(payload.get("received_at") or ""),
            "source_id": str(entity.get("canonical_key") or ""),
            "source_version": str(entity.get("version") or ""),
            "checksum": str(entity.get("checksum") or ""),
            "payload_json": entity.get("payload_json") or {},
        })
    return lots


# ---------------------------------------------------------------------------
# readiness snapshot computation
# ---------------------------------------------------------------------------

def _compute_snapshot(*, order_id: str, tenant_id: str,
                      observed_at: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Deterministic order-level readiness snapshot from canonical facts.

    Returns (data, meta) where meta carries {status, review flags, reason…}
    mirrored into data.  Never raises for missing authority — an "unknown"
    snapshot is the structured result instead (legacy service convention).
    """
    calculated_at = observed_at
    notes: list[str] = []

    order_found = _find_order(order_id, tenant_id)
    order_entity, find_notes = order_found
    notes.extend(find_notes)

    products, product_notes = _order_demand_products(order_entity)
    notes.extend(product_notes)

    completeness = {"bom": False, "inventory": False, "in_transit": False,
                    "substitution": False, "uom": False}
    input_versions = {
        "engineering_release_id": "",
        "bom_version": "",
        "wms_inventory_snapshot_id": "",
        "wms_inventory_snapshot_version": "",
        "m4_supply_snapshot_id": "",
        "m4_supply_snapshot_version": "",
        "substitution_policy_version": "",
    }

    if order_entity is None:
        # structured unknown (order may exist upstream but not yet canonical)
        if "canonical_store_unavailable" in notes:
            reason, message = "CANONICAL_UNAVAILABLE", "canonical 库存库未配置（YUNPAI_M0_DB）；无法读取订单/库存事实"
            recovery = "配置 YUNPAI_M0_DB 指向 canonical 库后重试"
        else:
            reason, message = "ORDER_NOT_FOUND", f"canonical 库中无订单 {order_id}"
            recovery = "先完成订单文件解析/M0 canonical 发布后再查询齐套"
        unknown = _unknown_snapshot(
            order_id=order_id, observed_at=observed_at, calculated_at=calculated_at,
            reason=reason, message=message, recovery=recovery,
                        checksum_source={"order_id": order_id, "tenant_id": tenant_id,
                             "missing": [f"order:{order_id}"], "notes": notes},
        )
        return unknown, {"status": "unknown", "reason": reason}

    order_key = str(order_entity["entity"].get("canonical_key") or "")
    input_versions["engineering_release_id"] = order_key

    if not products:
        reason = "ORDER_FACTS_INCOMPLETE"
        unknown = _unknown_snapshot(
            order_id=order_id, observed_at=observed_at, calculated_at=calculated_at,
            reason=reason,
            message=f"订单 {order_id} 缺产品编码/可量化数量（canonical payload 无 product_code 或 lines[]）",
            recovery="补全订单 canonical 事实（product_code + order_qty/行数量）后重试",
                        checksum_source={"order_id": order_id, "tenant_id": tenant_id,
                             "entity": order_entity["entity"].get("payload_json"),
                             "notes": notes},
        )
        return unknown, {"status": "unknown", "reason": reason}

    boms_all = _canonical_entities("bom", tenant_id)
    boms = boms_all if isinstance(boms_all, list) else []
    bom_found = False
    bom_metas: list[dict[str, Any]] = []
    used_payloads: list[Any] = []
    if order_entity is not None:
        used_payloads.append(order_entity["entity"].get("payload_json"))

    demand: dict[str, dict[str, Any]] = {}
    bom_missing_products: list[str] = []
    for product in products:
        code = product["product_code"]
        if not code:
            continue
        rows, meta = _bom_for_product(code, tenant_id, boms)
        if meta is None:
            bom_missing_products.append(code)
            continue
        bom_found = True
        if not rows and meta.get("empty_lines"):
            bom_missing_products.append(code)
            continue
        bom_metas.append(meta)
        if isinstance(meta.get("payload_json"), dict):
            used_payloads.append(meta["payload_json"])
        if product["qty"] <= 0:
            notes.append(f"product_zero_qty:{code}")
        for row in rows:
            code_m = row["material_code"]
            if row["qty_per"] <= 0:
                notes.append(f"bom_zero_qty_per:{code_m}")
                continue
            item = demand.setdefault(code_m, {
                "material_code": code_m,
                "material_name": row["material_name"],
                "uom": row["uom"],
                "required": 0.0,
            })
            if row["uom"] and not item["uom"]:
                item["uom"] = row["uom"]
            item["required"] += product["qty"] * row["qty_per"] * (1.0 + row["loss_rate"])

    if not bom_found or bom_missing_products:
        reason = "BOM_MISSING"
        unknown = _unknown_snapshot(
            order_id=order_id, observed_at=observed_at, calculated_at=calculated_at,
            reason=reason,
            message=f"产品 {','.join(bom_missing_products or [products[0]['product_code']])} 无可用 canonical BOM 行",
            recovery="先完成 BOM 文件解析/M0 canonical 发布，或经 M2 确认受控 BOM",
                        checksum_source={"order_id": order_id, "tenant_id": tenant_id,
                             "entity": order_entity["entity"].get("payload_json"),
                             "products": products, "missing_bom": bom_missing_products,
                             "notes": notes},
        )
        return unknown, {"status": "unknown", "reason": reason}

    if not demand:
        reason = "BOM_EMPTY" if not any(
            (b.get("payload_json") or {}).get("lines") for b in bom_metas) else "QUANTITY_INVALID"
        unknown = _unknown_snapshot(
            order_id=order_id, observed_at=observed_at, calculated_at=calculated_at,
            reason=reason,
            message=f"订单 {order_id} 产品 BOM 无有效用量行（全零/缺 qty_per）",
            recovery="确认 BOM 用量（qty_per>0）后重试",
                        checksum_source={"order_id": order_id, "tenant_id": tenant_id,
                             "entity": order_entity["entity"].get("payload_json"),
                             "notes": notes},
        )
        return unknown, {"status": "unknown", "reason": reason}
    completeness["bom"] = True

    # bom version evidence from the used BOM entities
    input_versions["bom_version"] = ",".join(
        str(m.get("version_id") or "") for m in bom_metas if m.get("version_id"))

    lots = _inventory_lots(tenant_id)
    if lots is None:
        completeness["inventory"] = False
        reason = "INVENTORY_UNAVAILABLE"
        unknown = _unknown_snapshot(
            order_id=order_id, observed_at=observed_at, calculated_at=calculated_at,
            reason=reason, message="canonical 库存读失败（库存在但查询异常）",
            recovery="检查 YUNPAI_M0_DB canonical 库状态后重试",
                        checksum_source={"order_id": order_id, "tenant_id": tenant_id,
                             "entity": order_entity["entity"].get("payload_json"),
                             "notes": notes},
        )
        return unknown, {"status": "unknown", "reason": reason}

    # inventory read succeeded (may legitimately be empty)
    completeness["inventory"] = True
    by_material: dict[str, list[dict[str, Any]]] = {}
    for lot in lots:
        by_material.setdefault(lot["material_code"], []).append(lot)
        used_payloads.append(lot["payload_json"])

    inventory_keys = sorted({lot["source_id"] for lot in lots if lot["source_id"]})
    inventory_versions = sorted({lot["source_version"] for lot in lots if lot["source_version"]})
    input_versions["wms_inventory_snapshot_id"] = ",".join(inventory_keys)
    input_versions["wms_inventory_snapshot_version"] = ",".join(inventory_versions)

    materials: list[dict[str, Any]] = []
    order_quality_hold = False
    order_shortage = False
    demand_uom_missing = False

    for material_code in sorted(demand):
        item = demand[material_code]
        required = item["required"]
        if not item["uom"]:
            demand_uom_missing = True
        lot_rows = by_material.get(material_code, [])

        usable = [lot for lot in lot_rows
                  if (not lot["qc_status"] or lot["qc_status"] in _RELEASED_QC)
                  and lot["available_qty"] - lot["locked_qty"] > _EPS]
        blocked = [lot for lot in lot_rows
                   if lot["qc_status"] and lot["qc_status"] not in _RELEASED_QC
                   and lot not in usable]
        # FIFO by received_at ascending (missing received_at treated as
        # available now, mirroring the orchestration bridge default)
        usable.sort(key=lambda lot: (lot["received_at"] or observed_at, lot["source_id"]))

        remaining = required
        allocated = 0.0
        allocations: list[dict[str, Any]] = []
        for lot in usable:
            if remaining <= _EPS:
                break
            take = min(remaining, lot["available_qty"] - lot["locked_qty"])
            if take <= _EPS:
                continue
            allocated += take
            remaining -= take
            allocations.append({
                "source_type": "wms_inventory_lot",
                "source_id": lot["source_id"] or f"{lot['warehouse']}|{lot['lot_no']}",
                "source_version": lot["source_version"],
                "quantity": _fmt_qty(take),
                "confidence": 1.0,
                "observed_at": observed_at,
                "checksum": lot["checksum"] or _sha256_text(
                    _canonical_json(lot["payload_json"]).decode("utf-8")),
            })
        shortage = max(0.0, required - allocated)

        if shortage > _EPS:
            order_shortage = True

        if shortage > _EPS and not usable and blocked:
            status = "quality_hold"
            order_quality_hold = True
            trusted_ready_at = None
        elif shortage > _EPS:
            status = "shortage"
            trusted_ready_at = None
        else:
            status = "ready"
            trusted_ready_at = observed_at if allocated > _EPS else None

        materials.append({
            "material_code": material_code,
            "material_name": item["material_name"],
            "uom": item["uom"],
            "required_quantity": _fmt_qty(required),
            "allocated_quantity": _fmt_qty(allocated),
            "shortage_quantity": _fmt_qty(shortage),
            "trusted_ready_at": trusted_ready_at,
            "status": status,
            "allocations": allocations,
        })

    completeness["uom"] = not demand_uom_missing

    # order-level status (manifest enum: ready|partial|shortage|quality_hold|unknown)
    covered_count = sum(1 for m in materials if m["status"] == "ready")
    if order_quality_hold:
        order_status = "quality_hold"
    elif order_shortage:
        # partial = at least one material fully covered while others are short;
        # shortage = no material fully covered
        order_status = "partial" if covered_count else "shortage"
    else:
        order_status = "ready"

    earliest_kitting_time = observed_at if order_status == "ready" else None

    input_checksum = _sha256_text(_canonical_json(
        {"order": order_entity["entity"].get("payload_json"),
         "used": used_payloads,
         "demand": sorted(demand)}).decode("utf-8"))

    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": f"M3-READY-{order_id}-{input_checksum[:8]}",
        "snapshot_version": "1",  # replaced by persisted ordinal in the handler
        "calculation_version": CALCULATION_VERSION,
        "input_versions": input_versions,
        "calculated_at": calculated_at,
        "observed_at": observed_at,
        "orders": [{
            "order_id": order_id,
            "status": order_status,
            "earliest_kitting_time": earliest_kitting_time,
            "materials": materials,
        }],
        "completeness": completeness,
        "input_checksum": input_checksum,
    }
    if order_quality_hold:
        data["recoverable"] = True
        data["review_required"] = True
        data["reason_code"] = "QUALITY_HOLD"
        data["message"] = f"订单 {order_id} 存在仅质检不可用库存 lot 支撑的物料，需质检确认"
        data["recovery_hint"] = "确认/放行质检状态或补充合格库存后重算"
    meta = {"status": order_status, "order": order_entity}
    return data, meta


def _unknown_snapshot(*, order_id: str, observed_at: str, calculated_at: str,
                      reason: str, message: str, recovery: str,
                      checksum_source: Any) -> dict[str, Any]:
    input_checksum = _sha256_text(_canonical_json(checksum_source).decode("utf-8"))
    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": f"M3-READY-{order_id}-{input_checksum[:8]}",
        "snapshot_version": "1",
        "calculation_version": CALCULATION_VERSION,
        "input_versions": {
            "engineering_release_id": "",
            "bom_version": "",
            "wms_inventory_snapshot_id": "",
            "wms_inventory_snapshot_version": "",
            "m4_supply_snapshot_id": "",
            "m4_supply_snapshot_version": "",
            "substitution_policy_version": "",
        },
        "calculated_at": calculated_at,
        "observed_at": observed_at,
        "orders": [{
            "order_id": order_id,
            "status": "unknown",
            "earliest_kitting_time": None,
            "materials": [],
        }],
        "completeness": {"bom": False, "inventory": False,
                         "in_transit": False, "substitution": False,
                         "uom": False},
        "input_checksum": input_checksum,
        "recoverable": True,
        "review_required": True,
        "reason_code": reason,
        "message": message,
        "recovery_hint": recovery,
    }
    return data


def _next_snapshot_version(store: M3Store, order_id: str, tenant_id: str) -> str:
    latest = store.latest_snapshot(order_id, tenant_id)
    if latest is None:
        return "v1"
    try:
        return f"v{int(str(latest['snapshot_version']).lstrip('v')) + 1}"
    except (TypeError, ValueError):
        return "v1"


def _legacy_block(tool: str) -> dict[str, Any]:
    """LEGACY 工具的逐条决策块（单一声明源在 ``m3_m4_tooling``）。

    V2 侧 13 个 LEGACY 工具由 M3 HTTP 服务承接（不迁本地 handler），本函数保留为
    决策块读取口，供后续按需在本地结果里附稳定提示（``M3_LEGACY_DECISIONS`` 已随迁）。
    """
    decision = M3_LEGACY_DECISIONS.get(tool)
    if not decision:
        return {}
    return {
        "deprecated": decision["status"] == "deprecated",
        "deprecation": {
            "tool": tool,
            "status": decision["status"],
            "replacement": decision["replacement"],
            "reason": decision["reason"],
            "outcome": decision["outcome"],
            "failure_code": decision["failure_code"],
        },
    }


# ---------------------------------------------------------------------------
# formal handler
# ---------------------------------------------------------------------------

async def get_material_readiness_snapshot(payload: dict[str, Any],
                                          ctx: dict[str, Any]) -> dict[str, Any]:
    """正式物料齐套快照（按 m3.json 契约本地化）。

    Reads canonical order/bom/inventory facts and returns an order-level
    readiness snapshot; missing authority yields a structured unknown
    snapshot (recoverable).  Same (order_id, tenant_id, input_checksum)
    replays the persisted snapshot (same snapshot_id/observed_at).
    """
    order_id = str((payload or {}).get("order_id") or "").strip()
    tenant_id = str((payload or {}).get("tenant_id") or ctx.get("tenant_id") or "default").strip()
    if not order_id:
        observed_at = _now_iso()
        unknown = _unknown_snapshot(
            order_id="", observed_at=observed_at, calculated_at=observed_at,
            reason="INVALID_INPUT", message="order_id 必填",
            recovery="提供目标订单 ID（order_id）后重试",
            checksum_source={"invalid_input": "missing_order_id", "tenant_id": tenant_id},
        )
        return {
            "success": False,
            "code": "INVALID_INPUT",
            "data": unknown,
            "errors": [_err("INVALID_INPUT", "order_id 必填")],
            "trace_id": _trace(ctx, "readiness-snapshot"),
        }
    store = M3Store(_store_path(ctx))
    observed_at = _now_iso()
    data, meta = _compute_snapshot(order_id=order_id, tenant_id=tenant_id,
                                   observed_at=observed_at)
    data["snapshot_version"] = _next_snapshot_version(store, order_id, tenant_id)
    input_checksum = str(data["input_checksum"] or "")
    stored = store.store_snapshot(
        order_id=order_id, tenant_id=tenant_id,
        input_checksum=input_checksum,
        snapshot_id=str(data["snapshot_id"]),
        snapshot_version=str(data["snapshot_version"]),
        calculation_version=CALCULATION_VERSION,
        order_status=str(data["orders"][0]["status"]),
        observed_at=observed_at,
        payload=data,
    )
    return {
        "success": True,
        "data": stored["payload"] if stored else data,
        "errors": [],
        "trace_id": _trace(ctx, "readiness-snapshot"),
    }


# ---------------------------------------------------------------------------
# R4: contract-branch helpers (m2_package conversion + open PO visibility).
# run_m3_procurement_requirements 的 handler 在 workers.py（本模块只提供确定性
# 转换/汇总函数，接线由 workers.m3_mrp 调用）。
# ---------------------------------------------------------------------------

_M2_ORDER_REQUIRED = ("project_id", "order_id", "bom_id", "product_name", "order_qty", "due_date")


def m2_package_to_order_bom(m2_package: dict[str, Any]) -> dict[str, Any]:
    """把 M2 兼容包（bom_header/bom_lines）转成正式 {order, bom} 形状。

    manifest 的 ``anyOf`` 允许 ``m2_package`` 作为 ``order``+``bom`` 的替代，
    但本地 handler 只读 ``order``/``bom``，因此该分支实测直接 BLOCKED_INPUT。
    这里给出确定性转换（不做任何推测填充）：缺字段抛 ``ValueError``，由调用方
    转成契约化 ``BLOCKED_INPUT``。
    """
    if not isinstance(m2_package, dict):
        raise ValueError("m2_package 必须是对象")
    header = m2_package.get("bom_header")
    if isinstance(header, list):
        header = header[0] if header else {}
    header = header if isinstance(header, dict) else {}
    lines = m2_package.get("bom_lines")
    if not isinstance(lines, list) or not lines:
        raise ValueError("m2_package.bom_lines 不能为空")

    bom_id = str(header.get("bom_id") or header.get("bom_code") or "")
    product_name = str(header.get("product_name") or header.get("name") or "")
    project = m2_package.get("project_file") if isinstance(m2_package.get("project_file"), dict) else {}
    order_id = str(header.get("order_id") or project.get("order_id") or "")
    if not bom_id:
        raise ValueError("m2_package.bom_header.bom_id 缺失")
    if not order_id:
        raise ValueError("m2_package 缺 order_id（bom_header.order_id 或 project_file.order_id）")

    converted: list[dict[str, Any]] = []
    for index, line in enumerate(lines, start=1):
        if not isinstance(line, dict):
            continue
        qty_per = _num_strict(line.get("qty_per") if line.get("qty_per") not in (None, "")
                              else line.get("quantity_per") if line.get("quantity_per") not in (None, "")
                              else line.get("quantity"))
        if qty_per is None or qty_per <= 0:
            raise ValueError(f"m2_package.bom_lines[{index}].qty_per 必须为正数")
        converted.append({
            "line_id": str(line.get("line_id") or line.get("item_no") or f"line-{index}"),
            "material_code": str(line.get("material_code") or line.get("item_code") or ""),
            "material_name": str(line.get("material_name") or line.get("name") or line.get("material_code") or ""),
            "qty_per": qty_per,
            "uom": str(line.get("uom") or line.get("unit") or "pcs"),
            "loss_rate": _num(line.get("loss_rate")),
            "requires_procurement": bool(line.get("requires_procurement", True)),
        })
    if not converted:
        raise ValueError("m2_package.bom_lines 无可量化行")

    order_qty = _num_strict(header.get("order_qty") if header.get("order_qty") not in (None, "")
                            else header.get("quantity"))
    if order_qty is None or order_qty <= 0:
        raise ValueError("m2_package.bom_header.order_qty 必须为正数")
    due_date = str(header.get("due_date") or project.get("due_date") or "")
    if not due_date:
        raise ValueError("m2_package 缺 due_date（bom_header.due_date 或 project_file.due_date）")
    project_id = str(header.get("project_id") or project.get("project_id") or order_id)
    resolved = {
        "project_id": project_id,
        "order_id": order_id,
        "bom_id": bom_id,
        "product_name": product_name or bom_id,
        "order_qty": order_qty,
        "due_date": due_date,
    }
    missing = [field for field in _M2_ORDER_REQUIRED if not resolved.get(field)]
    if missing:
        raise ValueError(f"m2_package 缺正式 order 字段: {', '.join(sorted(missing))}")

    return {
        "order": dict(resolved),
        "bom": {"bom_id": bom_id, "product_name": resolved["product_name"], "lines": converted},
    }


def open_po_quantities(payload: dict[str, Any]) -> dict[str, float]:
    """按物料汇总 ``open_purchase_orders[*].open_po_qty``（在途采购）。

    只做确定性汇总，语义边界明确（不猜交期/供应商），供 ``workers.m3_mrp`` 接入：
    ``shortage = max(0, gross - available - open_po)``。
    """
    totals: dict[str, float] = {}
    for item in (payload or {}).get("open_purchase_orders") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("material_code") or "")
        if not code:
            continue
        totals[code] = totals.get(code, 0.0) + _num(item.get("open_po_qty"))
    return totals


#: 本模块注册的本地工具（rows-S4 判定「改造后搬」的唯一一个；其余 16 件为
#: 保留HTTP/不搬/废弃，见模块头注）。
LOCAL_HANDLERS: dict[str, Any] = {
    "get_material_readiness_snapshot": get_material_readiness_snapshot,
}
