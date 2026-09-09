"""M0 canonical 进程内事实读服务（S1 Batch A）。
> 迁入自 `_wt/INT/src/yunpai_langgraph/m0_facts.py`（LF 归一 sha256=b2ac3c17b388cef9ce104e921a29b020567ae6d03234649c85fc761198b8e8db，10611B / 208 行）。
> M0 分片 C1 随迁（`_migration/rows-S1.md`）；本文件改动：② `_envelope_fields` 增加 attributes 归一化（父会话裁决 · M0 读口一致性）。

替代 m0_catalog urllib 直连 / bridge HTTP 回读的本地数据来源：
- 读面：M0Store(list_entities) + canonical payload 规范化；
- 配置：环境变量 YUNPAI_M0_DB 指向 canonical sqlite 库（v3 副本或新库）；
  未设置时自动注入关闭（保持测试与旧行为 hermetic），不隐式读取 runtime 大库。
- 租户：严格按调用方 tenant_id 过滤（与 RLS 语义一致）；
  跨租户归并（consolidated 源）在副本归并完成后另行加入。
"""
from __future__ import annotations

import os
from typing import Any

from .fact_gateway import split_envelope


def _store_path() -> str | None:
    path = os.getenv("YUNPAI_M0_DB") or ""
    if path and os.path.exists(path):
        return path
    return None


def _normalize_line(line: dict[str, Any]) -> dict[str, Any]:
    """canonical BOM 行 -> M2 请求风格行（保留原键 + quantity_per 别名）。"""
    out = {
        "material_code": str(line.get("material_code") or ""),
        "material_name": str(line.get("material_name") or line.get("material_code") or "未命名物料"),
        "quantity_per": line.get("quantity_per") if line.get("quantity_per") is not None else line.get("quantity"),
        "unit": line.get("unit"),
        "specification": line.get("specification"),
        "note": line.get("note"),
    }
    return {key: value for key, value in out.items() if value is not None and value != ""}


def list_entities(entity_type: str, tenant_id: str = "default") -> list[dict[str, Any]]:
    """按实体类型读回 canonical 实体（envelope 展开为 dict 列表，bridge HTTP 形状兼容）。

    形状与 m0_backend /api/m0/catalog/entities 一致：{"canonical_key": ..., **envelope}。
    """
    path = _store_path()
    if not path:
        return []
    try:
        from .m0_backend import M0Store

        result = M0Store(path).list_entities(entity_type, tenant_id=tenant_id)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for entity in result.get("entities", []):
        envelope = entity.get("payload_json")
        if not isinstance(envelope, dict):
            continue
        out.append({"canonical_key": entity.get("canonical_key"), **envelope})
    return out


def _envelope_fields(envelope: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(inner payload, identity) 提取；兼容 v3 envelope 与平铺记录。

    V2 读口归一化（M0 分片）：`business_catalog` 落库的 canonical 记录把
    ``route_steps`` 等业务事实放在 ``payload.attributes`` 下
    （`business_catalog.py:1131-1138`），而 envelope 写入方放在 ``payload`` 顶层。
    统一口径「attributes 的键并入顶层、同名以顶层优先、attributes 原键保留」
    由 ``fact_gateway.split_envelope`` 实现（集成收口 1.1，单点化）。
    """
    return split_envelope(envelope)


def _matches_product(envelope: dict[str, Any], product_code: str) -> bool:
    payload, identity = _envelope_fields(envelope)
    code = str(identity.get("business_key") or payload.get("product_code") or envelope.get("product_code") or "")
    if code == product_code:
        return True
    lines = payload.get("lines")
    if isinstance(lines, list):
        return any(str(line.get("product_code") or "") == product_code for line in lines if isinstance(line, dict))
    codes = payload.get("product_codes")
    if isinstance(codes, list):
        return product_code in {str(c) for c in codes}
    return False


def _bom_envelopes(product_code: str, tenant_id: str) -> list[dict[str, Any]]:
    """该产品匹配到的 canonical BOM 信封（产品视图与 BOM 行读取的唯一匹配口径）。

    P1-10：``product_overview`` / ``product_graph`` / ``bom_lines`` 若各自实现
    产品匹配，同一产品在不同读面上会得到不同结果；统一收在此函数。
    """
    if not product_code:
        return []
    return [item for item in list_entities("bom", tenant_id=tenant_id)
            if _matches_product(item, product_code)]


def inventory_rows(tenant_id: str = "default", material_code: str | None = None,
                   limit: int | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in list_entities("inventory", tenant_id=tenant_id):
        payload, identity = _envelope_fields(item)
        code = str(identity.get("business_key") or payload.get("material_code") or "")
        if material_code and code != material_code:
            continue
        row = {"material_code": code}
        row.update({k: v for k, v in payload.items() if k not in row})
        out.append(row)
    return out[: int(limit)] if limit else out


def document_rows(tenant_id: str = "default", doc_type: str | None = None,
                  product_code: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in list_entities("document", tenant_id=tenant_id):
        payload, identity = _envelope_fields(item)
        role = str(payload.get("role") or identity.get("business_key") or "")
        if doc_type and doc_type.upper() not in {role.upper(), str(payload.get("doc_type") or "").upper()}:
            continue
        if product_code and not _matches_product(item, product_code):
            continue
        row = {"document_key": str(identity.get("business_key") or item.get("canonical_key") or ""), "role": role}
        row.update({k: v for k, v in payload.items() if k not in row})
        out.append(row)
    return out[: int(limit)] if limit else out


def product_overview(product_code: str, tenant_id: str = "default") -> dict[str, Any]:
    """产品视图的唯一实现（P1-10 保留项）：订单/BOM 各修订/文档索引 + 计数。

    ``get_m0_product_overview`` 与 ``product_graph`` 都经此函数；BOM 匹配口径
    统一走 ``_bom_envelopes``，不再各写一份。
    """
    boms: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    product_found = None
    for item in list_entities("product", tenant_id=tenant_id):
        if str(item.get("canonical_key") or "") == product_code:
            product_found = item.get("canonical_key")
    for item in _bom_envelopes(product_code, tenant_id):
        payload, identity = _envelope_fields(item)
        lines = payload.get("lines")
        boms.append({"canonical_key": item.get("canonical_key"), "version": item.get("version"),
                     "lines": len(lines) if isinstance(lines, list) else 0,
                     "product_code": str(identity.get("business_key") or payload.get("product_code") or product_code)})
    for item in list_entities("order", tenant_id=tenant_id):
        if _matches_product(item, product_code):
            orders.append({"canonical_key": item.get("canonical_key"), "version": item.get("version")})
    for item in list_entities("document", tenant_id=tenant_id):
        if _matches_product(item, product_code):
            documents.append({"canonical_key": item.get("canonical_key"), "version": item.get("version")})
    return {"product_code": product_code, "found": bool(product_found or boms or orders or documents),
            "product": product_found, "boms": boms, "orders": orders, "documents": documents,
            "counts": {"bom": len(boms), "order": len(orders), "document": len(documents)}}


def product_graph(product_code: str, tenant_id: str = "default", depth: int = 2) -> dict[str, Any]:
    """产品关系图（**deprecated**，P1-10）。

    .. deprecated::
       主链无消费方（``get_m0_product_graph`` 零调用）。产品事实请用
       :func:`product_overview`；BOM 行请用 :func:`bom_lines`。本函数仅为
       兼容 ``get_m0_product_graph`` 保留，depth 语义上限为 2：
       depth=1 只到 BOM/订单/文档节点，depth>=2 再展开 BOM 行 → 物料节点。
       depth=3 与 depth=2 等价（产品族/同系列展开未实现，契约已同步收窄）。
    """
    depth = 2 if int(depth or 2) > 2 else int(depth or 2)
    overview = product_overview(product_code, tenant_id)
    nodes = [{"key": product_code, "type": "product"}]
    edges: list[dict[str, Any]] = []
    for bom in overview["boms"]:
        nodes.append({"key": bom["canonical_key"], "type": "bom"})
        edges.append({"from": product_code, "to": bom["canonical_key"], "relation": "has_bom"})
        if depth > 1:
            for item in list_entities("bom", tenant_id=tenant_id):
                if item.get("canonical_key") == bom["canonical_key"]:
                    payload, _ = _envelope_fields(item)
                    for line in payload.get("lines") or []:
                        if isinstance(line, dict) and line.get("material_code"):
                            nodes.append({"key": str(line["material_code"]), "type": "material"})
                            edges.append({"from": bom["canonical_key"], "to": str(line["material_code"]), "relation": "uses_material"})
    for order in overview["orders"]:
        nodes.append({"key": order["canonical_key"], "type": "order"})
        edges.append({"from": order["canonical_key"], "to": product_code, "relation": "contains_product"})
    for doc in overview["documents"]:
        nodes.append({"key": doc["canonical_key"], "type": "document"})
        edges.append({"from": product_code, "to": doc["canonical_key"], "relation": "has_document"})
    seen: set[tuple[str, str]] = set()
    unique_nodes = [n for n in nodes
                    if not ((n["key"], n["type"]) in seen or seen.add((n["key"], n["type"])))]
    return {"product_code": product_code, "depth": depth,
            "nodes": unique_nodes, "edges": edges}


def bom_lines(product_code: str, tenant_id: str = "default") -> list[dict[str, Any]]:
    """按产品编码读取 canonical BOM 行（进程内只读）；无库/无匹配返回 []。

    P1-10：产品匹配统一走 :func:`_bom_envelopes`，与 ``product_overview`` 同口径。
    """
    if not product_code:
        return []
    for envelope in _bom_envelopes(str(product_code), tenant_id):
        payload, _identity = _envelope_fields(envelope)
        lines = payload.get("lines")
        if not isinstance(lines, list):
            continue
        return [_normalize_line(line) for line in lines if isinstance(line, dict)]
    return []
