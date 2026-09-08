"""M1 HTTP adapter transport acceptance: headers, multipart, 202, errors, filters."""

from __future__ import annotations

import base64

import httpx
import pytest

import yunpai_orchestrator.m1_http_adapter as adapter
from yunpai_orchestrator.registry import ToolHTTPError, build_default_registry

from m1_fake_http import BASE, Backend, install, status_error

CTX = {"task_id": "TASK-1", "run_id": "RUN-1", "tenant_id": "TENANT-1"}


def _registry():
    registry = build_default_registry()
    adapter.bind_m1_http(registry, urls={"m1": BASE}, overwrite=True)
    return registry


@pytest.mark.asyncio
async def test_multipart_upload_keeps_file_bytes_and_form_fields(monkeypatch):
    backend = Backend()
    backend.route("POST", "/ingest/sync", {"task_id": "task-1", "status": "done"})
    install(monkeypatch, backend)
    registry = _registry()
    raw = b"%PDF-1.4 fake order"
    await registry.call("ingest_document", {
        "file": {"filename": "order.pdf", "content_type": "application/pdf", "content_b64": base64.b64encode(raw).decode()},
        "doc_type_hint": "order",
        "semantic_enrichment": True,
    }, CTX)
    call = backend.calls[0]
    field, file_tuple = call["files"][0]
    assert field == "file"
    assert file_tuple == ("order.pdf", raw, "application/pdf")
    assert call["data"]["doc_type_hint"] == "order"
    assert call["headers"]["X-Tenant-ID"] == "TENANT-1"
    assert call["headers"]["X-Yunpai-Tenant-ID"] == "TENANT-1"
    assert call["headers"]["X-Yunpai-Task-ID"] == "TASK-1"


@pytest.mark.asyncio
async def test_tenant_header_mapping_never_silently_defaults(monkeypatch):
    backend = Backend()
    backend.route("GET", "/tasks", [])
    install(monkeypatch, backend)
    registry = _registry()
    await registry.call("list_m1_tasks", {}, {"task_id": "T", "tenant_id": "ACME"})
    assert backend.calls[0]["headers"]["X-Tenant-ID"] == "ACME"
    with pytest.raises(ToolHTTPError, match="MISSING_TENANT"):
        await registry.call("list_m1_tasks", {}, {"task_id": "T"})
    assert len(backend.calls) == 1  # the failing call never reached the network


@pytest.mark.asyncio
async def test_actor_roles_only_from_trusted_context(monkeypatch):
    backend = Backend()
    backend.route("GET", "/knowledge/search", {"tenant_id": "TENANT-1", "query": "q", "include_candidate": False, "hits": []}, repeat=True)
    install(monkeypatch, backend)
    registry = _registry()
    await registry.call("search_m1_knowledge", {"q": "q"}, {"task_id": "T", "tenant_id": "TENANT-1", "actor_id": "u-1", "actor_roles": ["reviewer", "admin"]})
    headers = backend.calls[-1]["headers"]
    assert headers["X-Actor-ID"] == "u-1"
    assert headers["X-Actor-Roles"] == "reviewer,admin"

    # Self-reported roles inside a client payload must NOT propagate.
    await registry.call("search_m1_knowledge", {"q": "q", "actor_roles": ["reviewer"]}, {"task_id": "T", "tenant_id": "TENANT-1"})
    assert "X-Actor-Roles" not in backend.calls[-1]["headers"]


@pytest.mark.asyncio
async def test_env_roles_can_authorize_service_account(monkeypatch):
    monkeypatch.setenv("M1_ACTOR_ROLES", "admin")
    backend = Backend()
    stats = {
        "tenant_id": "TENANT-1",
        "inventory": {"ingestion_batches": {"total": 0}, "source_assets": {"total": 0}, "source_occurrences": {"total": 0}, "documents": {"total": 0}, "document_versions": {"total": 0}, "task_version_bindings": {"total": 0}, "canonical_entities": {"total": 0}, "external_identities": {"total": 0}, "entity_aliases": {"total": 0}, "projection_outbox": {"total": 0}},
        "claims": {},
        "projections": {"chunks": {"total": 0, "by_status": {}}, "index_documents": {"total": 0, "by_status": {}}, "graph_snapshots": {"total": 0, "by_status": {}}},
    }
    backend.route("GET", "/knowledge/stats", stats)
    install(monkeypatch, backend)
    registry = _registry()
    result = await registry.call("get_m1_knowledge_stats", {}, {"task_id": "T", "tenant_id": "TENANT-1"})
    assert result["tenant_id"] == "TENANT-1"
    assert backend.calls[-1]["headers"]["X-Actor-Roles"] == "admin"


@pytest.mark.asyncio
async def test_sync_ingest_202_bounded_poll_returns_terminal_result(monkeypatch):
    backend = Backend()
    backend.route("POST", "/ingest/sync", {
        "code": "M1_INGEST_ACCEPTED", "message": "processing continues", "task_id": "task-1",
        "status": "parsing", "processing_stage": "parsing", "sync_complete": False,
        "poll_url": "/tasks/task-1",
    }, status_code=202)
    backend.route("GET", "/tasks/task-1", {
        "task_id": "task-1", "status": "parsing", "processing_stage": "parsing",
    })
    backend.route("GET", "/tasks/task-1", {
        "task_id": "task-1", "status": "done", "processing_stage": "complete",
        "needs_review": False, "overall_confidence": 0.99, "document_schema_version": "m1.document.v2",
    })
    install(monkeypatch, backend)
    registry = _registry()
    raw = b'{"order_id": "SO-1"}'
    result = await registry.call("ingest_document", {
        "file": {"filename": "order.json", "content_b64": base64.b64encode(raw).decode()},
    }, CTX)
    assert result["status"] == "done"
    paths = [call["path"] for call in backend.calls]
    assert paths == ["/ingest/sync", "/tasks/task-1", "/tasks/task-1"]


@pytest.mark.asyncio
async def test_sync_ingest_202_beyond_budget_returns_explicit_pending(monkeypatch):
    monkeypatch.setattr(adapter, "POLL_BUDGET_S", 0.0)
    backend = Backend()
    backend.route("POST", "/ingest/sync", {
        "code": "M1_INGEST_ACCEPTED", "message": "processing continues", "task_id": "task-9",
        "status": "parsing", "processing_stage": "parsing", "sync_complete": False,
        "poll_url": "/tasks/task-9",
    }, status_code=202)
    install(monkeypatch, backend)
    registry = _registry()
    result = await registry.call("ingest_document", {
        "file": {"filename": "order.pdf", "content_b64": base64.b64encode(b"%PDF").decode()},
    }, CTX)
    assert result["status"] == "pending"
    assert result["task_id"] == "task-9"
    assert result["poll"] is True
    assert result["document"] is None
    assert "轮询" in result["message"]


@pytest.mark.asyncio
async def test_status_errors_map_to_stable_tool_error_without_leaking(monkeypatch):
    class _Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def request(self, method, url, **kwargs):
            raise status_error(method, url, 404, "资源不存在")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    registry = _registry()
    with pytest.raises(ToolHTTPError) as error:
        await registry.call("get_m1_task", {"task_id": "missing"}, CTX)
    assert error.value.code == "HTTP_STATUS_ERROR"
    assert error.value.status_code == 404
    assert "资源不存在" in str(error.value)
    assert "default" not in str(error.value)


@pytest.mark.asyncio
async def test_timeout_maps_to_stable_tool_error(monkeypatch):
    class _Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def request(self, method, url, **kwargs):
            raise httpx.ReadTimeout("upstream", request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    registry = _registry()
    with pytest.raises(ToolHTTPError, match="HTTP_TIMEOUT"):
        await registry.call("get_m1_task", {"task_id": "t"}, CTX)


@pytest.mark.asyncio
async def test_order_search_applies_manifest_attribute_filters_client_side(monkeypatch):
    backend = Backend()
    backend.route("GET", "/orders/search", [
        {
            "task_id": "task-1", "line_id": "L-1", "line_no": 1, "order_number": "SO-1",
            "document_date": "2026-09-01", "model": "", "product_code": "W-1",
            "name_raw": "HDMI 2.0 20M", "name_normalized": "HDMI 2.0 20M", "full_product_name": "",
            "product_category": "cable", "quantity": 5, "unit": "PCS",
            "line": {"line_id": "L-1", "name_attributes": {"interface": "HDMI", "cable_length": "20M", "plug": "Type-C", "conductor": "CCA", "od": "7.0mm"}, "color": "black"},
        },
        {
            "task_id": "task-2", "line_id": "L-2", "line_no": 1, "order_number": "SO-2",
            "document_date": "2026-09-01", "model": "", "product_code": "W-2",
            "name_raw": "USB 3.0", "name_normalized": "USB 3.0", "full_product_name": "",
            "product_category": "cable", "quantity": 2, "unit": "PCS",
            "line": {"line_id": "L-2", "name_attributes": {"interface": "USB"}},
        },
    ])
    install(monkeypatch, backend)
    registry = _registry()
    result = await registry.call(
        "search_m1_orders",
        {"interface": "HDMI", "length": "20M", "connector": "Type-C", "conductor": "CCA", "od": "7.0mm", "limit": 50, "offset": 0},
        CTX,
    )
    assert [row["task_id"] for row in result] == ["task-1"]
