"""M1 review queue/report/export acceptance over the HTTP mock backend."""

from __future__ import annotations

import pytest

import yunpai_orchestrator.m1_http_adapter as adapter
from yunpai_orchestrator.registry import ToolHTTPError, build_default_registry

from m1_fake_http import BASE, Backend, install

CTX = {"task_id": "TASK-ROOT", "tenant_id": "TENANT-1"}


def _registry():
    registry = build_default_registry()
    adapter.bind_m1_http(registry, urls={"m1": BASE}, overwrite=True)
    return registry


@pytest.mark.asyncio
async def test_review_queue_lists_only_tenant_leaf_tasks(monkeypatch):
    backend = Backend()
    backend.route("GET", "/review/queue", [
        {"task_id": "child-2", "filename": "b.pdf", "overall_confidence": 0.71},
    ], repeat=True)
    install(monkeypatch, backend)
    registry = _registry()
    queue = await registry.call("list_m1_review_queue", {"limit": 20, "offset": 0}, CTX)
    assert [item["task_id"] for item in queue] == ["child-2"]
    # Batch parents are excluded by the service; the adapter passes pagination
    # through as query parameters.
    assert backend.calls[-1]["params"] == {"limit": 20, "offset": 0}


@pytest.mark.asyncio
async def test_review_submission_preserves_corrections_and_polls_to_done(monkeypatch):
    backend = Backend()
    backend.route("POST", "/review/task-r", {
        "code": "M1_REVIEW_ACCEPTED", "message": "saved", "task_id": "task-r",
        "status": "reviewing", "processing_stage": "review_indexing", "sync_complete": False,
        "poll_url": "/tasks/task-r",
    }, status_code=202)
    backend.route("GET", "/tasks/task-r", {
        "task_id": "task-r", "status": "done", "processing_stage": "complete",
        "needs_review": False, "overall_confidence": 1.0,
    })
    install(monkeypatch, backend)
    registry = _registry()
    result = await registry.call("submit_m1_review", {
        "task_id": "task-r", "approve": False, "reviewer": "reviewer-1", "comment": "fix qty",
        "corrections": {"lines.0.quantity": "9"},
    }, CTX)
    body = backend.calls[0]["json"]
    assert body["approve"] is False
    assert body["reviewer"] == "reviewer-1"
    assert body["corrections"] == {"lines.0.quantity": "9"}
    assert result["status"] == "done"


@pytest.mark.asyncio
async def test_review_pending_never_looks_completed(monkeypatch):
    monkeypatch.setattr(adapter, "POLL_BUDGET_S", 0.0)
    backend = Backend()
    backend.route("POST", "/review/task-r2", {
        "code": "M1_REVIEW_ACCEPTED", "task_id": "task-r2", "status": "parsing",
        "processing_stage": "review_indexing", "sync_complete": False, "poll_url": "/tasks/task-r2",
    }, status_code=202)
    install(monkeypatch, backend)
    registry = _registry()
    result = await registry.call("submit_m1_review", {"task_id": "task-r2", "approve": True}, CTX)
    assert result["status"] == "pending"
    assert result["poll"] is True


@pytest.mark.asyncio
async def test_report_and_export_return_metadata_not_server_paths(monkeypatch):
    backend = Backend()
    backend.route("POST", "/tasks/t-3/report", {
        "task_id": "t-3", "report_kind": "single", "report_status": "ready", "message": "ok",
    })
    backend.route("GET", "/tasks/t-4/exports/order", {
        "task_id": "t-4", "filename": "SO-1_order.xlsx",
        "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "download_url": f"{BASE}/tasks/t-4/exports/order.xlsx", "generated": True,
    })
    install(monkeypatch, backend)
    registry = _registry()
    report = await registry.call("generate_m1_report", {"task_id": "t-3", "note": "for review", "force": True}, CTX)
    assert report["report_kind"] == "single"
    exported = await registry.call("export_m1_order", {"task_id": "t-4"}, CTX)
    assert exported["content_type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert exported["download_url"].startswith(f"{BASE}/tasks/t-4/exports/order.xlsx")
    # Adapter never returns or fabricates absolute server file paths.
    assert "/data/" not in exported["download_url"]
    assert not exported["download_url"].startswith("file://")


@pytest.mark.asyncio
async def test_report_on_missing_or_foreign_task_maps_to_stable_error(monkeypatch):
    from m1_fake_http import status_error

    class _Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def request(self, method, url, **kwargs):
            raise status_error(method, url, 404, "资源不存在")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    registry = _registry()
    with pytest.raises(ToolHTTPError) as error:
        await registry.call("generate_m1_report", {"task_id": "other-tenant-task"}, CTX)
    assert error.value.code == "HTTP_STATUS_ERROR"
    assert error.value.status_code == 404
    assert "资源不存在" in str(error.value)
