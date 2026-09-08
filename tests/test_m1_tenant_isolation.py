"""M1 tenant isolation acceptance: X-Tenant-ID propagation and fail-closed."""

from __future__ import annotations

import httpx
import pytest

import yunpai_orchestrator.m1_http_adapter as adapter
from yunpai_orchestrator.registry import ToolHTTPError, build_default_registry

from m1_fake_http import BASE, Backend, install, status_error


def _registry():
    registry = build_default_registry()
    adapter.bind_m1_http(registry, urls={"m1": BASE}, overwrite=True)
    return registry


@pytest.mark.asyncio
async def test_two_tenants_never_share_or_default_tenant_header(monkeypatch):
    backend = Backend()
    backend.route("GET", "/tasks", [], repeat=True)
    backend.route("GET", "/orders/search", [], repeat=True)
    install(monkeypatch, backend)
    registry = _registry()
    await registry.call("list_m1_tasks", {}, {"task_id": "T", "tenant_id": "ACME"})
    await registry.call("search_m1_orders", {"limit": 10, "offset": 0}, {"task_id": "T", "tenant_id": "GLOBALTECH"})
    assert backend.calls[0]["headers"]["X-Tenant-ID"] == "ACME"
    assert backend.calls[1]["headers"]["X-Tenant-ID"] == "GLOBALTECH"
    assert {call["headers"]["X-Tenant-ID"] for call in backend.calls} == {"ACME", "GLOBALTECH"}


@pytest.mark.asyncio
async def test_missing_tenant_fails_closed_before_any_request(monkeypatch):
    backend = Backend()
    install(monkeypatch, backend)
    registry = _registry()
    with pytest.raises(ToolHTTPError, match="MISSING_TENANT"):
        await registry.call("get_m1_task", {"task_id": "task-1"}, {"task_id": "T"})
    assert backend.calls == []


@pytest.mark.asyncio
async def test_payload_tenant_is_accepted_when_context_omits_it(monkeypatch):
    backend = Backend()
    backend.route("GET", "/tasks", [], repeat=True)
    install(monkeypatch, backend)
    registry = _registry()
    # list_m1_tasks has no tenant_id input field, but a tenant_id in the payload
    # (used by orchestrator-wide callers) is honored without going to default.
    await registry.call("list_m1_tasks", {"tenant_id": "TENANT-PAYLOAD"}, {"task_id": "T"})
    assert backend.calls[0]["headers"]["X-Tenant-ID"] == "TENANT-PAYLOAD"


@pytest.mark.asyncio
async def test_cross_tenant_access_returns_stable_404_without_existence_leak(monkeypatch):
    class _Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def request(self, method, url, **kwargs):
            # Service deliberately returns 404 (not 403) for cross-tenant reads.
            raise status_error(method, url, 404, "资源不存在")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    registry = _registry()
    with pytest.raises(ToolHTTPError) as error:
        await registry.call("get_m1_task", {"task_id": "task-other-tenant"}, {"task_id": "T", "tenant_id": "ACME"})
    assert error.value.code == "HTTP_STATUS_ERROR"
    assert error.value.status_code == 404
    message = str(error.value)
    assert "资源不存在" in message
    assert "task-other-tenant" not in message  # no object-existence detail leaked
