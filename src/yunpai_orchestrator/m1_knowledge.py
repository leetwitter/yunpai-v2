"""M1 知识链（S2 M1-3）：search/list/get/graph/stats 五个工具的真实本地实现。

随迁自 ``_wt/INT/src/yunpai_langgraph/m1_knowledge.py``（351 行；父会话裁定
2026-09-09 批准：该文件含 5 个 ``async def handler(payload, ctx)``，按
``INFRA-DECISIONS.md §2.2`` 粒度判据属**业务实现模块**，函数由归属分片迁——
M-INFRA 只漏建骨架。逻辑保持零改动，仅按 V2 读口裁决改数据访问，见下）。

事实源（V2 读口裁决，父会话 2026-09-09）：
- **标准实体 = canonical 库，一律经 ``m0_backend.M0Store.list_entities()``**
  （``m0_backend.py:197-224``，返回 ``{entities:[{entity_id, entity_type,
  canonical_key, version, payload_json(已解析), checksum}]}``）。**禁止**直连
  sqlite3 读 ``canonical_entities``/``canonical_entity_versions``（INT 版即那样
  写，本文件已改）；
- 关系 = ``m0_catalog_relations``（status=active）；**M0Store 目前没有关系边
  读 API**，暂经 ``_m0_conn()`` 直读并做租户过滤，已登记为跨分片诉求（见
  ``_migration/REQUESTS-MIG-M1.md``）；
- 任务/审核计数 = m1_domain 库（``YUNPAI_M1_DB``，缺省
  ``runtime/yunpai-m1.sqlite``）。

本地等价登记（与旧 Governed Wiki/向量/Neo4j 的差距，R030）：
- search：精确身份命中（canonical_key/编码字段）得分 1.0/exact_identifier；
  关键词命中 0.6/keyword；无向量/图分（0）；candidate 永不暴露
  （include_candidate 恒 false——schema const）；
- graph：projection = canonical_key 当前版本快照 + 关系边表一阶邻接
  （nodes/edges 均 status=active）；
- stats：各计数为本地表真实行数映射（旧 wiki 专属表无对应 → 诚实填 0 并注记）。
输出全部按 manifest output_schema 直返（handler 返回即契约形状）。

形状归一（父会话 M0 读口裁决第 2 条）：``business_catalog`` 落库的实体把业务
字段放在 ``payload.attributes`` 下（SOP document 的 ``route_steps`` 等），
``_normalize_body()`` 把 attributes 的键并入顶层、**顶层优先**，位置在
``_as_item`` / ``knowledge_search`` / ``knowledge_graph_get`` 的 body 提取处。

租户/权限（R2 修复沿用）：实体读由 ``M0Store.list_entities(entity_type,
tenant_id)`` 做 ``WHERE e.tenant_id=? AND e.lifecycle_status='active'``；关系读
经 owner 实体 join 做租户过滤（``m0_catalog_relations`` 本身无 tenant_id 列）；
candidate/under_review 行永不暴露；跨租户或不存在一律 fail-closed 抛
``ValueError``（等价 HTTP 404）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from .m1_domain import M1Store

# 知识搜索覆盖的 canonical entity_type（标准实体集合）
KNOWLEDGE_TYPES: tuple[str, ...] = (
    "product", "product_family", "order", "bom", "material", "supplier",
    "equipment", "process_route", "operation", "tooling", "document",
)


def _m0_db_path() -> str:
    """M0 canonical 库路径（V2 读口裁决：与 ``M0Store`` 缺省口径一致）。"""
    return str(os.getenv("YUNPAI_M0_DB") or "runtime/yunpai-m0.sqlite")


def _m0_store():
    """M0 canonical 读口（唯一允许的 canonical_entities 读路径）。"""
    from .m0_backend import M0Store

    return M0Store(_m0_db_path())


def _m0_conn() -> sqlite3.Connection:
    """**仅**用于 ``M0Store.list_entities`` 未覆盖的表（关系边 / 入库计数）。

    TODO(跨分片)：V2 ``M0Store`` 尚无 ``list_relations`` 或计数读 API
    （``INFRA-DECISIONS.md §2.4`` 已建议补 ``M0Store.list_relations``）；补好
    后本函数及其调用点应一并删除。canonical_entities/canonical_entity_versions
    **不得**再经此连接读取。
    """
    db = sqlite3.connect(_m0_db_path())
    db.row_factory = sqlite3.Row
    return db


def _entity_rows(tenant_id: str, entity_type: str | None = None,
                 lifecycle_status: str = "active") -> list[dict[str, Any]]:
    """租户内的 canonical 实体（当前版本），按 lifecycle_status 过滤。

    ``lifecycle_status`` 是 ``list_m1_knowledge_entities`` 的**必填契约字段**
    （``registry-manifests/m1.json`` ``const="active"``）：``M0Store.list_entities``
    只返回 active 当前版本，故请求非 active 时诚实返回空集，**不伪造**。

    ``entity_type`` 为空时按 ``KNOWLEDGE_TYPES`` 逐个读并合并（``list_entities``
    要求显式 entity_type），结果按 (entity_type, canonical_key) 稳定排序。
    """
    if lifecycle_status != "active":
        return []
    store = _m0_store()
    types = (entity_type,) if entity_type else KNOWLEDGE_TYPES
    rows: list[dict[str, Any]] = []
    for etype in types:
        payload = store.list_entities(etype, tenant_id)
        for item in payload.get("entities") or []:
            rows.append({
                "entity_id": item["entity_id"],
                "tenant_id": tenant_id,
                "entity_type": item["entity_type"],
                "canonical_key": item["canonical_key"],
                "current_version": item["version"],
                "lifecycle_status": "active",
                "payload_json": item.get("payload_json"),
            })
    rows.sort(key=lambda row: (str(row["entity_type"]), str(row["canonical_key"])))
    return rows


def _payload_of(row: dict[str, Any]) -> dict[str, Any]:
    """``payload_json`` 归一：M0Store 已解析为 dict，INT 版是字符串。"""
    payload = row.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except ValueError:
            payload = {}
    return payload if isinstance(payload, dict) else {}


def _normalize_body(payload: dict[str, Any]) -> dict[str, Any]:
    """``payload.attributes`` 键并入顶层、顶层优先（business_catalog 形状归一）。"""
    body = payload.get("payload") if isinstance(payload.get("payload"), dict) else payload
    if not isinstance(body, dict):
        return {}
    attributes = body.get("attributes")
    if isinstance(attributes, dict):
        return {**attributes, **body}
    return body


def _flatten_text(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten_text(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(_flatten_text(v) for v in value)
    return str(value)


def _label_of(payload: dict[str, Any], fallback: str) -> str:
    for key in ("name", "title", "product_name", "material_name", "document_no",
                "product_code", "order_id", "model", "canonical_label"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return fallback


def _as_item(row: dict[str, Any], payload: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    body = _normalize_body(payload)
    return {
        "entity_id": row["entity_id"], "tenant_id": tenant_id,
        "entity_type": row["entity_type"], "canonical_key": row["canonical_key"],
        "canonical_label": _label_of(body if isinstance(body, dict) else payload,
                                    row["canonical_key"]),
        "attributes": {"payload": payload, "version": row["current_version"]},
        "lifecycle_status": str(row.get("lifecycle_status") or "active"),
        "confidence": 1.0,
        "acl": {"tenant_id": tenant_id, "mode": "tenant_isolation"},
    }


async def knowledge_list_entities(payload: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    tenant = str(ctx.get("tenant_id") or "default")
    entity_type = str(payload.get("entity_type") or "") or None
    # 必填契约字段（const=active）：真正参与过滤，不再忽略。
    lifecycle_status = str(payload.get("lifecycle_status") or "active")
    limit = int(payload.get("limit") or 100)
    offset = int(payload.get("offset") or 0)
    rows = _entity_rows(tenant, entity_type=entity_type,
                        lifecycle_status=lifecycle_status)
    items = [_as_item(r, _payload_of(r), tenant) for r in rows]
    return items[offset:offset + limit]


async def knowledge_entity_get(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    tenant = str(ctx.get("tenant_id") or "default")
    entity_id = str(payload.get("entity_id") or "")
    if not entity_id:
        raise ValueError("get_m1_knowledge_entity 需要 entity_id")
    rows = [r for r in _entity_rows(tenant) if r["entity_id"] == entity_id]
    if not rows:
        raise ValueError(f"entity not found: {entity_id}")
    row = rows[0]
    payload_doc = _payload_of(row)
    item = _as_item(row, payload_doc, tenant)
    item["valid_from"] = None
    item["valid_to"] = None
    return item


async def knowledge_search(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    tenant = str(ctx.get("tenant_id") or "default")
    q = str(payload.get("q") or "").strip()
    if not q:
        raise ValueError("search_m1_knowledge 需要 q")
    doc_type = str(payload.get("document_type") or "") or None
    mode = str(payload.get("mode") or "precise")
    min_score = float(payload.get("min_score") or 0.0)
    limit = int(payload.get("limit") or 20)
    ql = q.lower()
    rows = _entity_rows(tenant, entity_type=doc_type)
    hits: list[dict[str, Any]] = []
    for row in rows:
        payload_doc = _payload_of(row)
        body = _normalize_body(payload_doc)
        key = row["canonical_key"].lower()
        exact = ql == key or ql in {str(v).lower() for v in
                                    (body if isinstance(body, dict) else {}).values()
                                    if isinstance(v, (str, int, float))}
        if exact:
            score = 1.0
            reason = "exact_identifier"
        elif mode == "broad" and ql in _flatten_text(payload_doc).lower():
            score = 0.6
            reason = "keyword"
        else:
            continue
        if score < min_score:
            continue
        selected_fields: dict[str, Any] = {}
        flat = body if isinstance(body, dict) else {}
        for k in ("order_id", "product_code", "material_code", "name", "title",
                  "document_no", "role", "model", "quantity", "due_date"):
            if flat.get(k) is not None:
                selected_fields[k] = flat[k]
        hit_doc = {
            "tenant_id": tenant,
            "source_record_id": row["canonical_key"],
            "version": str(row["current_version"]),
            "document_type": row["entity_type"],
            "status": "active",
            "selected_fields": selected_fields,
        }
        hits.append({
            "index_id": f"{tenant}:{row['entity_type']}:{row['canonical_key']}",
            "record_id": row["entity_id"], "score": score,
            "keyword_score": score if reason == "keyword" else 0.0,
            "vector_score": 0.0, "graph_score": 0.0,
            "match_reason": reason,
            "document": hit_doc,
        })
        if len(hits) >= limit:
            break
    return {"tenant_id": tenant, "query": q, "include_candidate": False,
            "mode": mode, "min_score": min_score, "hits": hits}


def _relation_rows(tenant_id: str) -> list[dict[str, Any]]:
    """租户内的活跃关系边。

    ``m0_catalog_relations`` 没有 tenant_id 列，租户归属由 owner 实体决定，因此
    必须 join ``canonical_entities`` 过滤；否则别的租户的关系边会污染本租户的
    图投影与 stats 计数（R2 修复：跨租户泄漏）。

    **V2 缺口（实测）**：V2 ``src/`` 内**没有任何代码创建
    ``m0_catalog_relations``**（该表来自 INT 的 ``m0_catalog_ingest.py``，未随迁），
    且 ``M0Store`` 也没有关系边读 API。表不存在时诚实返回空集（关系事实不存在，
    不伪造边），已登记到 ``_migration/REQUESTS-MIG-M1.md``。
    """
    with _m0_conn() as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='m0_catalog_relations'"
        ).fetchone()
        if exists is None:
            return []
        rows = db.execute(
            "SELECT r.* FROM m0_catalog_relations r "
            "JOIN canonical_entities e ON e.canonical_key=r.owner_entity_key "
            "AND e.entity_type=r.owner_entity_type "
            "WHERE r.status='active' AND e.tenant_id=? AND e.lifecycle_status='active'",
            (tenant_id,)).fetchall()
    return [dict(r) for r in rows]


async def knowledge_graph_get(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    tenant = str(ctx.get("tenant_id") or "default")
    source_record_id = str(payload.get("source_record_id") or "")
    if not source_record_id:
        raise ValueError("get_m1_knowledge_graph 需要 source_record_id")
    rows = [r for r in _entity_rows(tenant) if r["canonical_key"] == source_record_id]
    if not rows:
        raise ValueError(f"source record not found: {source_record_id}")
    seed = rows[0]
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []

    def add_node(row: dict[str, Any]) -> str:
        node_id = f"{row['entity_type']}:{row['canonical_key']}"
        if node_id not in nodes:
            payload_doc = _payload_of(row)
            body = _normalize_body(payload_doc)
            nodes[node_id] = {
                "node_id": node_id,
                "labels": [row["entity_type"]],
                "status": "active",
                "confidence": 1.0,
                "properties": {"canonical_key": row["canonical_key"],
                               "version": row["current_version"],
                               "canonical_label": _label_of(body if isinstance(body, dict) else payload_doc,
                                                            row["canonical_key"])},
            }
        return node_id

    seed_id = add_node(seed)
    by_key = {f"{r['entity_type']}:{r['canonical_key']}": r for r in _entity_rows(tenant)}
    for rel in _relation_rows(tenant):
        skey = f"{rel['source_type']}:{rel['source_key']}"
        tkey = f"{rel['target_type']}:{rel['target_key']}"
        if skey == seed_id or tkey == seed_id:
            for nid in (skey, tkey):
                if nid in by_key:
                    add_node(by_key[nid])
            edges.append({
                "edge_id": rel["relation_id"], "relation": rel["relation_type"],
                "from_node": skey, "to_node": tkey, "status": "active",
                "confidence": 1.0,
                "properties": {"derived_from": rel.get("derived_from") or "explicit",
                               "owner": f"{rel['owner_entity_type']}:{rel['owner_entity_key']}"},
            })
    version = str(seed["current_version"])
    return {"projection_id": f"proj:{tenant}:{source_record_id}:{version}",
            "tenant_id": tenant, "source_record_id": source_record_id, "version": version,
            "nodes": list(nodes.values()), "edges": edges}


async def knowledge_stats(payload: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    tenant = str(ctx.get("tenant_id") or "default")
    rows = _entity_rows(tenant)

    def inv_count(total: int, by_status: dict[str, int] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"total": total}
        if by_status:
            out["by_status"] = by_status
        return out

    def _count(table: str, extra: str = "") -> int:
        sql = f"SELECT count(*) AS n FROM {table} WHERE tenant_id=? {extra}"
        with _m0_conn() as db:
            row = db.execute(sql, (tenant,)).fetchone()
        return int(row["n"])

    def _count_joined(table: str, on: str) -> int:
        with _m0_conn() as db:
            row = db.execute(
                f"SELECT count(*) AS n FROM {table} t JOIN import_batches b {on} "
                "WHERE b.tenant_id=?", (tenant,)).fetchone()
        return int(row["n"])

    def self_outbox_count(tenant_id: str) -> int:
        with _m0_conn() as db:
            row = db.execute(
                "SELECT count(*) AS n FROM canonical_outbox t "
                "WHERE t.ledger_id IN (SELECT l.ledger_id FROM canonical_ledger l "
                "JOIN import_batches b2 ON b2.batch_id=l.batch_id WHERE b2.tenant_id=?)",
                (tenant_id,)).fetchone()
        return int(row["n"])

    versions_total = 0
    doc_count = 0
    for r in rows:
        versions_total += int(r["current_version"])
        if r["entity_type"] == "document":
            doc_count += 1
    rel_rows = _relation_rows(tenant)
    rel_by_type: dict[str, int] = {}
    for rel in rel_rows:
        rel_by_type[rel["relation_type"]] = rel_by_type.get(rel["relation_type"], 0) + 1
    m1 = M1Store()
    review_count = sum(1 for t in m1.list_tasks(tenant, status="needs_review", limit=10000)
                       if t["kind"] != "child")
    inventory = {
        "ingestion_batches": inv_count(_count("import_batches")),
        "source_assets": inv_count(_count_joined("source_documents", "ON t.batch_id=b.batch_id")),
        "source_occurrences": inv_count(0),
        "documents": inv_count(doc_count),
        "document_versions": inv_count(versions_total),
        "task_version_bindings": inv_count(sum(
            1 for t in m1.list_tasks(tenant, limit=10000)
            if t["kind"] != "child" and t["status"] in ("done", "needs_review"))),
        "canonical_entities": inv_count(len(rows)),
        "external_identities": inv_count(0),
        "entity_aliases": inv_count(0),
        "projection_outbox": inv_count(self_outbox_count(tenant)),
    }
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["entity_type"]] = by_type.get(r["entity_type"], 0) + 1
    claims = {"field_claims": by_type, "relationship_claims": rel_by_type,
              "open_review_tasks": review_count, "open_conflicts": 0}
    graph_nodes = sum(1 for r in rows) + len(rel_rows)
    projections = {
        "chunks": {"total": 0, "by_status": {}},
        "index_documents": {"total": len(rows), "by_status": {"active": len(rows)}},
        "graph_snapshots": {"total": graph_nodes, "by_status": {"active": graph_nodes}},
        "outbox": {},
    }
    return {"tenant_id": tenant, "inventory": inventory, "claims": claims,
            "projections": projections}
