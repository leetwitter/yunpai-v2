"""M1 lifecycle acceptance: 17 tools round-trip over an HTTP mock backend.

Every ``registry.call`` below validates the returned JSON against the tool's
output schema, so a mock body that does not satisfy the manifest contract fails
the test — this is the offline contract matrix for the M1 module.
"""

from __future__ import annotations

import base64

import pytest

import yunpai_orchestrator.m1_http_adapter as adapter
from yunpai_orchestrator.m1_tooling import M1_TOOL_NAMES
from yunpai_orchestrator.registry import ToolHTTPError, build_default_registry

from m1_fake_http import BASE, Backend, install

CTX = {"task_id": "TASK-ROOT", "run_id": "RUN-ROOT", "tenant_id": "TENANT-1"}

DOC_V2 = {
    "schema_version": "m1.document.v2",
    "source": {"original_filename": "order.pdf", "sha256": "ab" * 32, "mime_type": "application/pdf"},
    "document_type": "order",
    "document_subtype": "customer_order",
    "order_type": "normal",
    "header": {
        "order_number": "SO-1",
        "currency": "CNY",
        "date_standard": "2026-09-01",
        "date_inferred": False,
        "delivery_date_standard": "2026-09-10",
    },
    "lines": [
        {"line_id": "L-1", "line_no": 1, "product_code": "W-1", "name_raw": "HDMI 2.0 20M", "name_normalized": "HDMI 2.0 20M", "quantity": 5, "unit": "PCS", "name_attributes": {"interface": "HDMI", "cable_length": "20M"}},
    ],
    "totals": {"declared_quantity": 5, "calculated_quantity": 5, "quantity_matches": True},
    "field_meta": {"$.header.order_number": {"confidence": 0.99, "source_refs": [{"page": 1}]}},
    "validation_issues": [],
    "needs_review": False,
}

STATS = {
    "tenant_id": "TENANT-1",
    "inventory": {
        "ingestion_batches": {"total": 1}, "source_assets": {"total": 1}, "source_occurrences": {"total": 1},
        "documents": {"total": 1}, "document_versions": {"total": 1}, "task_version_bindings": {"total": 1},
        "canonical_entities": {"total": 1}, "external_identities": {"total": 1}, "entity_aliases": {"total": 1},
        "projection_outbox": {"total": 0},
    },
    "claims": {"field_claims": {"$.header.order_number": 1}},
    "projections": {
        "chunks": {"total": 2, "by_status": {"active": 2}},
        "index_documents": {"total": 1, "by_status": {"active": 1}},
        "graph_snapshots": {"total": 1, "by_status": {"active": 1}},
    },
}


def _registry():
    registry = build_default_registry()
    adapter.bind_m1_http(registry, urls={"m1": BASE}, overwrite=True)
    return registry


def _pdf(name: str = "order.pdf", content: bytes = b"%PDF-1.4 sample order") -> dict:
    return {"filename": name, "content_type": "application/pdf", "content_b64": base64.b64encode(content).decode()}


def _install_lifecycle_backend() -> Backend:
    backend = Backend()
    # ingest_m1_archive -> parent task with children
    backend.route("POST", "/ingest/archive", {
        "task_id": "parent-1", "status": "processing", "processing_stage": "archive_extracting",
        "child_count": 2, "child_ids": ["child-1", "child-2"], "async": True,
    })
    backend.route("GET", "/batch/parent-1", {
        "parent": {"task_id": "parent-1", "status": "done"},
        "children": [
            {"task_id": "child-1", "status": "done", "filename": "a.pdf"},
            {"task_id": "child-2", "status": "needs_review", "filename": "b.pdf"},
        ],
        "child_count": 2, "done_count": 1, "failed_count": 0, "review_count": 1, "pending_count": 0,
    })
    # ingest_document sync terminal
    backend.route("POST", "/ingest/sync", {
        "task_id": "task-sync", "status": "done", "processing_stage": "complete",
        "needs_review": False, "overall_confidence": 0.98,
        "document_schema_version": "m1.document.v2", "document": DOC_V2,
    })
    # read a persisted task + document
    backend.route("GET", "/tasks/t-1", {
        "task_id": "t-1", "status": "done", "processing_stage": "complete",
        "needs_review": False, "overall_confidence": 0.98,
        "document_schema_version": "m1.document.v2", "document": DOC_V2,
    }, repeat=True)
    backend.route("GET", "/tasks/t-1/document", DOC_V2, repeat=True)
    # searches/lists
    backend.route("GET", "/orders/search", [
        {"task_id": "t-1", "line_id": "L-1", "line_no": 1, "order_number": "SO-1", "document_date": "2026-09-01",
         "model": "", "product_code": "W-1", "name_raw": "HDMI 2.0 20M", "name_normalized": "HDMI 2.0 20M",
         "full_product_name": "", "product_category": "cable", "quantity": 5, "unit": "PCS",
         "line": {"line_id": "L-1", "name_attributes": {"interface": "HDMI"}}},
    ], repeat=True)
    backend.route("GET", "/documents/search", [
        {"task_id": "t-1", "schema_version": "m1.document.v2", "document_type": "order",
         "document_subtype": "customer_order", "title": None, "order_number": "SO-1",
         "normalized_date": "2026-09-01", "filename": "order.pdf", "sha256": "ab" * 32},
    ], repeat=True)
    backend.route("GET", "/tasks", [
        {"task_id": "t-1", "filename": "order.pdf", "status": "done", "needs_review": False, "overall_confidence": 0.98},
    ], repeat=True)
    backend.route("GET", "/review/queue", [
        {"task_id": "child-2", "filename": "b.pdf", "overall_confidence": 0.72},
    ], repeat=True)
    # review submission returns 202 and the polling endpoint reaches done
    backend.route("POST", "/review/t-2", {
        "code": "M1_REVIEW_ACCEPTED", "message": "indexing continues", "task_id": "t-2",
        "status": "parsing", "processing_stage": "review_indexing", "sync_complete": False,
        "poll_url": "/tasks/t-2",
    }, status_code=202)
    backend.route("GET", "/tasks/t-2", {"task_id": "t-2", "status": "parsing", "processing_stage": "review_indexing"})
    backend.route("GET", "/tasks/t-2", {"task_id": "t-2", "status": "done", "processing_stage": "complete", "document_schema_version": "m1.document.v2", "document": DOC_V2})
    # report + export metadata only (never raw server paths)
    backend.route("POST", "/tasks/t-3/report", {
        "task_id": "t-3", "report_kind": "single", "report_status": "ready", "message": "报告已生成",
    })
    backend.route("GET", "/tasks/t-4/exports/order", {
        "task_id": "t-4", "filename": "SO-1_order.xlsx",
        "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "download_url": f"{BASE}/tasks/t-4/exports/order.xlsx", "generated": True,
    })
    # knowledge queries (active-only default)
    backend.route("GET", "/knowledge/search", {
        "tenant_id": "TENANT-1", "query": "SO-1", "include_candidate": False, "mode": "precise", "min_score": 0.0,
        "hits": [{"index_id": "idx-1", "record_id": "rec-1", "score": 0.9,
                  "document": {"tenant_id": "TENANT-1", "source_record_id": "doc-1", "version": "v1",
                               "document_type": "order", "document_subtype": "customer_order",
                               "status": "active", "selected_fields": {"header.order_number": "SO-1"}}}],
    }, repeat=True)
    backend.route("GET", "/knowledge/entities", [
        {"entity_id": "ent-1", "tenant_id": "TENANT-1", "entity_type": "product", "canonical_key": "W-1",
         "canonical_label": "HDMI 2.0 20M", "attributes": {}, "lifecycle_status": "active", "confidence": 0.99, "acl": {}},
    ], repeat=True)
    backend.route("GET", "/knowledge/entities/ent-1", {
        "entity_id": "ent-1", "tenant_id": "TENANT-1", "entity_type": "product", "canonical_key": "W-1",
        "canonical_label": "HDMI 2.0 20M", "attributes": {}, "lifecycle_status": "active", "confidence": 0.99,
        "acl": {}, "valid_from": None, "valid_to": None,
    }, repeat=True)
    backend.route("GET", "/knowledge/graph/doc-1", {
        "projection_id": "proj-1", "tenant_id": "TENANT-1", "source_record_id": "doc-1", "version": "v1",
        "nodes": [{"node_id": "n-1", "labels": ["Product"], "status": "active", "confidence": 0.99, "properties": {}}],
        "edges": [{"edge_id": "e-1", "relation": "PART_OF", "from_node": "n-1", "to_node": "n-2", "status": "active", "properties": {}}],
    }, repeat=True)
    backend.route("GET", "/knowledge/stats", STATS, repeat=True)
    return backend


@pytest.mark.asyncio
async def test_all_seventeen_m1_tools_roundtrip_over_http_mock(monkeypatch):
    backend = _install_lifecycle_backend()
    install(monkeypatch, backend)
    registry = _registry()

    archive = await registry.call("ingest_m1_archive", {"file": _pdf("docs.zip", b"PK\x03\x04fake")}, CTX)
    assert archive["task_id"] == "parent-1" and archive["child_count"] == 2

    batch = await registry.call("get_m1_batch", {"parent_id": "parent-1"}, CTX)
    assert batch["child_count"] == 2 and batch["review_count"] == 1

    ingested = await registry.call("ingest_document", {"file": _pdf()}, CTX)
    assert ingested["status"] == "done"

    task = await registry.call("get_m1_task", {"task_id": "t-1"}, CTX)
    assert task["status"] == "done"

    doc = await registry.call("get_m1_document", {"task_id": "t-1"}, CTX)
    assert doc["schema_version"] == "m1.document.v2"

    orders = await registry.call("search_m1_orders", {"order_number": "SO-1", "limit": 50, "offset": 0}, CTX)
    assert orders[0]["order_number"] == "SO-1"

    documents = await registry.call("search_m1_documents", {"q": "SO-1"}, CTX)
    assert documents[0]["task_id"] == "t-1"

    tasks = await registry.call("list_m1_tasks", {"status": "done"}, CTX)
    assert tasks[0]["status"] == "done"

    queue = await registry.call("list_m1_review_queue", {"limit": 20, "offset": 0}, CTX)
    assert queue[0]["task_id"] == "child-2"

    reviewed = await registry.call("submit_m1_review", {"task_id": "t-2", "approve": True, "comment": "ok"}, CTX)
    assert reviewed["status"] == "done"

    report = await registry.call("generate_m1_report", {"task_id": "t-3", "note": "n"}, CTX)
    assert report["report_kind"] == "single"

    exported = await registry.call("export_m1_order", {"task_id": "t-4"}, CTX)
    assert exported["filename"].endswith(".xlsx")
    assert exported["download_url"].startswith(BASE)
    assert "file://" not in exported["download_url"]

    knowledge = await registry.call("search_m1_knowledge", {"q": "SO-1"}, CTX)
    assert knowledge["include_candidate"] is False
    assert knowledge["hits"][0]["document"]["status"] == "active"

    entities = await registry.call("list_m1_knowledge_entities", {"lifecycle_status": "active", "entity_type": "product", "limit": 20, "offset": 0}, CTX)
    assert entities[0]["canonical_key"] == "W-1"

    entity = await registry.call("get_m1_knowledge_entity", {"entity_id": "ent-1"}, CTX)
    assert entity["entity_type"] == "product"

    graph = await registry.call("get_m1_knowledge_graph", {"source_record_id": "doc-1"}, CTX)
    assert graph["projection_id"] == "proj-1"

    stats = await registry.call("get_m1_knowledge_stats", {}, CTX)
    assert stats["inventory"]["documents"]["total"] == 1


@pytest.mark.asyncio
async def test_sync_timeout_then_recoverable_via_get_m1_task(monkeypatch):
    """A 202 on ingest must not be reported as completion; the task id lets the
    caller resume through get_m1_task even when the poll budget is tiny."""
    monkeypatch.setattr(adapter, "POLL_BUDGET_S", 0.0)
    backend = Backend()
    backend.route("POST", "/ingest/sync", {
        "code": "M1_INGEST_ACCEPTED", "task_id": "task-long", "status": "scoring",
        "processing_stage": "scoring", "sync_complete": False, "poll_url": "/tasks/task-long",
    }, status_code=202)
    install(monkeypatch, backend)
    registry = _registry()
    result = await registry.call("ingest_document", {"file": _pdf()}, CTX)
    assert result["status"] == "pending" and result["task_id"] == "task-long"

    # The same HTTP adapter polls the task endpoint in a follow-up read.
    backend.route("GET", "/tasks/task-long", {
        "task_id": "task-long", "status": "done", "processing_stage": "complete",
        "document_schema_version": "m1.document.v2", "document": DOC_V2,
    }, repeat=True)
    terminal = await registry.call("get_m1_task", {"task_id": "task-long"}, CTX)
    assert terminal["status"] == "done"


@pytest.mark.asyncio
async def test_batch_poll_contract_keeps_parent_child_accounting(monkeypatch):
    backend = Backend()
    backend.route("GET", "/batch/parent-x", {
        "parent": {"task_id": "parent-x", "status": "processing", "processing_stage": "archive_children_running"},
        "children": [
            {"task_id": "c1", "status": "done"},
            {"task_id": "c2", "status": "failed", "error": "parse error"},
            {"task_id": "c3", "status": "needs_review"},
            {"task_id": "c4", "status": "parsing"},
        ],
        "child_count": 4, "done_count": 1, "failed_count": 1, "review_count": 1, "pending_count": 1,
    }, repeat=True)
    install(monkeypatch, backend)
    registry = _registry()
    batch = await registry.call("get_m1_batch", {"parent_id": "parent-x"}, CTX)
    assert batch["done_count"] == 1 and batch["failed_count"] == 1
    assert batch["review_count"] == 1 and batch["pending_count"] == 1
