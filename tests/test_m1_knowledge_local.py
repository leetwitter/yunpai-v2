"""M1 知识链 5 个工具的本地验收（S2 M1-3「改造后搬」）。

随迁 ``m1_knowledge.py`` 后，canonical 事实一律经 **``m0_backend.M0Store.list_entities``**
读口（父会话 M0 读口裁决 2026-09-09：禁止直连 sqlite3 读
``canonical_entities``/``canonical_entity_versions``），并做
``payload.attributes`` 形状归一（attributes 键并入顶层、顶层优先）。

覆盖：search/list/get/graph/stats 的契约形状、candidate 永不暴露、
租户隔离与 fail-closed、``lifecycle_status`` 真实参与过滤、以及 17/17
M1 工具在默认 registry 里全部 **BOUND_LOCAL**。
"""

from __future__ import annotations

import pytest

from yunpai_orchestrator.binding import BindingStatus, compute_bindings
from yunpai_orchestrator.m0_backend import M0Store
from yunpai_orchestrator.m1_knowledge import knowledge_list_entities, knowledge_search
from yunpai_orchestrator.m1_tooling import M1_TOOL_NAMES
from yunpai_orchestrator.registry import build_default_registry

CTX = {"task_id": "TASK-ROOT", "run_id": "RUN-ROOT", "tenant_id": "TENANT-1"}

PRODUCTS = [
    {"entity_type": "product", "product_code": "W-1", "name": "HDMI 2.0 20M",
     "attributes": {"interface": "HDMI", "cable_length": "20M"}},
    {"entity_type": "product", "product_code": "W-2", "name": "USB-C 3.0"},
]


@pytest.fixture(autouse=True)
def _isolated_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("YUNPAI_M1_DB", str(tmp_path / "m1.sqlite"))
    monkeypatch.delenv("YUNPAI_M0_DB", raising=False)


@pytest.fixture
def registry():
    return build_default_registry()


def _m0_canonical(tmp_path, monkeypatch, records, tenant_id="TENANT-1"):
    """用 M0Store 自己的写口建 canonical 库（M1 知识链只经 list_entities 读）。"""
    path = tmp_path / "m0.sqlite"
    monkeypatch.setenv("YUNPAI_M0_DB", str(path))
    store = M0Store(str(path))
    batch = store.ingest(records, tenant_id=tenant_id, task_id="T-M0")
    store.publish(batch["batch_id"], actor="tester", human_override=True)
    return store


def test_all_seventeen_m1_tools_are_bound_local(registry):
    bindings = compute_bindings(registry)
    assert sorted(M1_TOOL_NAMES) == sorted(
        name for name in registry.specs if registry.specs[name].module == "m1")
    not_local = {name: bindings[name].value for name in M1_TOOL_NAMES
                 if bindings[name] is not BindingStatus.BOUND_LOCAL}
    assert not not_local, f"仍有非本地绑定的 M1 工具：{not_local}"


@pytest.mark.asyncio
async def test_knowledge_tools_read_canonical_through_m0_store(registry, tmp_path, monkeypatch):
    _m0_canonical(tmp_path, monkeypatch, PRODUCTS)
    entities = await registry.call("list_m1_knowledge_entities",
                                   {"lifecycle_status": "active", "limit": 10}, CTX)
    assert [item["canonical_key"] for item in entities] == ["W-1", "W-2"]
    # attributes 形状归一：business_catalog 的 attributes 键并入顶层后参与 label 推导。
    assert entities[0]["canonical_label"] == "HDMI 2.0 20M"

    entity = await registry.call("get_m1_knowledge_entity",
                                 {"entity_id": entities[0]["entity_id"]}, CTX)
    assert entity["canonical_key"] == "W-1"
    assert entity["valid_from"] is None and entity["valid_to"] is None
    assert entity["attributes"]["payload"]["attributes"]["interface"] == "HDMI"

    search = await registry.call("search_m1_knowledge", {"q": "W-1"}, CTX)
    assert search["include_candidate"] is False and search["mode"] == "precise"
    assert [hit["document"]["source_record_id"] for hit in search["hits"]] == ["W-1"]
    assert search["hits"][0]["match_reason"] == "exact_identifier"
    broad = await registry.call("search_m1_knowledge", {"q": "usb", "mode": "broad"}, CTX)
    assert [hit["match_reason"] for hit in broad["hits"]] == ["keyword"]

    graph = await registry.call("get_m1_knowledge_graph", {"source_record_id": "W-1"}, CTX)
    assert graph["projection_id"].startswith(f"proj:{CTX['tenant_id']}:W-1:")
    assert [node["node_id"] for node in graph["nodes"]] == ["product:W-1"]
    # V2 没有 m0_catalog_relations 表（无代码创建）→ 关系边诚实为空，不伪造。
    assert graph["edges"] == []

    stats = await registry.call("get_m1_knowledge_stats", {}, CTX)
    assert stats["inventory"]["canonical_entities"]["total"] == 2
    assert stats["claims"]["field_claims"] == {"product": 2}


@pytest.mark.asyncio
async def test_knowledge_tools_are_tenant_isolated(registry, tmp_path, monkeypatch):
    store = _m0_canonical(tmp_path, monkeypatch, PRODUCTS, tenant_id="TENANT-1")
    other = store.ingest([{"entity_type": "product", "product_code": "W-OTHER", "name": "别的租户"}],
                         tenant_id="TENANT-2", task_id="T-2")
    store.publish(other["batch_id"], actor="tester", human_override=True)

    mine = await registry.call("list_m1_knowledge_entities", {"lifecycle_status": "active"}, CTX)
    theirs = await registry.call("list_m1_knowledge_entities", {"lifecycle_status": "active"},
                                 {**CTX, "tenant_id": "TENANT-2"})
    assert [item["canonical_key"] for item in mine] == ["W-1", "W-2"]
    assert [item["canonical_key"] for item in theirs] == ["W-OTHER"]

    # 跨租户按 entity_id 直读 → fail-closed（等价 HTTP 404），不泄漏存在性。
    with pytest.raises(ValueError, match="entity not found"):
        await registry.call("get_m1_knowledge_entity", {"entity_id": mine[0]["entity_id"]},
                            {**CTX, "tenant_id": "TENANT-2"})


@pytest.mark.asyncio
async def test_knowledge_list_rejects_non_active_lifecycle_without_fabricating(registry, tmp_path, monkeypatch):
    """契约 ``const=active`` 会先在 ``registry.call`` 拦掉其它取值；handler 层也必须
    诚实返回空集，而不是忽略 ``lifecycle_status`` 后返回全部（R2 修复点）。"""
    _m0_canonical(tmp_path, monkeypatch, PRODUCTS)
    with pytest.raises(ValueError, match="'active' was expected"):
        await registry.call("list_m1_knowledge_entities", {"lifecycle_status": "archived"}, CTX)
    assert await knowledge_list_entities({"lifecycle_status": "archived"}, CTX) == []
    assert len(await knowledge_list_entities({"lifecycle_status": "active"}, CTX)) == 2


@pytest.mark.asyncio
async def test_knowledge_search_requires_query_and_fails_closed(registry, tmp_path, monkeypatch):
    _m0_canonical(tmp_path, monkeypatch, PRODUCTS)
    # 契约 minLength=1 先在 registry 层拦掉空 q；handler 层同样 fail-closed。
    with pytest.raises(ValueError, match="non-empty"):
        await registry.call("search_m1_knowledge", {"q": ""}, CTX)
    with pytest.raises(ValueError, match="需要 q"):
        await knowledge_search({}, CTX)
    with pytest.raises(ValueError, match="source record not found"):
        await registry.call("get_m1_knowledge_graph", {"source_record_id": "missing"}, CTX)
