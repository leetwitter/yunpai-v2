"""Governed Wiki (M1 knowledge) tools acceptance over the HTTP mock backend."""

from __future__ import annotations

import pytest

import yunpai_orchestrator.m1_http_adapter as adapter
from yunpai_orchestrator.registry import ToolHTTPError, build_default_registry

from m1_fake_http import BASE, Backend, install

CTX = {"task_id": "TASK-ROOT", "tenant_id": "TENANT-1"}

ACTIVE_HIT = {
    "index_id": "idx-1", "record_id": "rec-1", "score": 0.9,
    "document": {"tenant_id": "TENANT-1", "source_record_id": "doc-1", "version": "v1",
                 "document_type": "order", "document_subtype": "customer_order",
                 "status": "active", "selected_fields": {"header.order_number": "SO-1"}},
}


def _registry():
    registry = build_default_registry()
    adapter.bind_m1_http(registry, urls={"m1": BASE}, overwrite=True)
    return registry


@pytest.mark.asyncio
async def test_knowledge_search_defaults_to_active_only_without_candidate_flag(monkeypatch):
    backend = Backend()
    backend.route("GET", "/knowledge/search", {
        "tenant_id": "TENANT-1", "query": "SO-1", "include_candidate": False, "hits": [ACTIVE_HIT],
    }, repeat=True)
    install(monkeypatch, backend)
    registry = _registry()
    result = await registry.call("search_m1_knowledge", {"q": "SO-1", "limit": 10}, CTX)
    assert result["include_candidate"] is False
    assert all(hit["document"]["status"] == "active" for hit in result["hits"])
    # The adapter must not ask the service for candidate rows.
    assert "include_candidate" not in backend.calls[-1]["params"]


@pytest.mark.asyncio
async def test_candidate_access_requires_trusted_roles_and_maps_403_stably(monkeypatch):
    backend = Backend()
    backend.route("GET", "/knowledge/search", {
        "tenant_id": "TENANT-1", "query": "SO-1", "include_candidate": False, "hits": [],
    }, repeat=True)
    install(monkeypatch, backend)
    registry = _registry()
    # Reviewer context roles ride on X-Actor-Roles.
    result = await registry.call(
        "search_m1_knowledge", {"q": "SO-1"},
        {**CTX, "actor_id": "u-1", "actor_roles": ["reviewer"]},
    )
    assert result["hits"] == []
    assert backend.calls[-1]["headers"]["X-Actor-Roles"] == "reviewer"

    from m1_fake_http import status_error

    class _Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def request(self, method, url, **kwargs):
            raise status_error(method, url, 403, {"detail": "需要以下角色之一: admin, reviewer"})

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with pytest.raises(ToolHTTPError) as error:
        await registry.call("search_m1_knowledge", {"q": "SO-1"}, CTX)
    assert error.value.status_code == 403
    assert "需要以下角色之一" in str(error.value)


@pytest.mark.asyncio
async def test_knowledge_entity_graph_stats_match_store_shape(monkeypatch):
    backend = Backend()
    backend.route("GET", "/knowledge/entities", [
        {"entity_id": "ent-1", "tenant_id": "TENANT-1", "entity_type": "product", "canonical_key": "W-1",
         "canonical_label": "HDMI 2.0 20M", "attributes": {}, "lifecycle_status": "active", "confidence": 0.99, "acl": {}},
    ], repeat=True)
    backend.route("GET", "/knowledge/entities/ent-1", {
        "entity_id": "ent-1", "tenant_id": "TENANT-1", "entity_type": "product", "canonical_key": "W-1",
        "canonical_label": "HDMI 2.0 20M", "attributes": {"interface": "HDMI"}, "lifecycle_status": "active",
        "confidence": 0.99, "acl": {}, "valid_from": None, "valid_to": None,
    }, repeat=True)
    backend.route("GET", "/knowledge/graph/doc-1", {
        "projection_id": "proj-1", "tenant_id": "TENANT-1", "source_record_id": "doc-1", "version": "v1",
        "nodes": [{"node_id": "n-1", "labels": ["Order"], "status": "active", "properties": {}}],
        "edges": [{"edge_id": "e-1", "relation": "REFERENCES", "from_node": "n-1", "to_node": "n-2", "status": "active", "properties": {}}],
    }, repeat=True)
    backend.route("GET", "/knowledge/stats", {
        "tenant_id": "TENANT-1",
        "inventory": {"ingestion_batches": {"total": 0}, "source_assets": {"total": 0}, "source_occurrences": {"total": 0}, "documents": {"total": 1}, "document_versions": {"total": 1}, "task_version_bindings": {"total": 1}, "canonical_entities": {"total": 1}, "external_identities": {"total": 1}, "entity_aliases": {"total": 1}, "projection_outbox": {"total": 0}},
        "claims": {"field_claims": {"$.header.order_number": 1}},
        "projections": {"chunks": {"total": 2, "by_status": {"active": 2}}, "index_documents": {"total": 1, "by_status": {"active": 1}}, "graph_snapshots": {"total": 1, "by_status": {"active": 1}}},
    }, repeat=True)
    install(monkeypatch, backend)
    registry = _registry()
    entities = await registry.call("list_m1_knowledge_entities", {"lifecycle_status": "active", "entity_type": "product", "limit": 20, "offset": 0}, CTX)
    assert entities[0]["entity_id"] == "ent-1"
    entity = await registry.call("get_m1_knowledge_entity", {"entity_id": "ent-1"}, CTX)
    assert entity["attributes"]["interface"] == "HDMI"
    graph = await registry.call("get_m1_knowledge_graph", {"source_record_id": "doc-1"}, CTX)
    assert graph["nodes"][0]["status"] == "active"
    stats = await registry.call("get_m1_knowledge_stats", {}, CTX)
    assert stats["projections"]["index_documents"]["total"] == 1
    # Projections and active knowledge are not M0 canonical by themselves; the
    # tool output carries no publish semantics (asserted implicitly by schema).
